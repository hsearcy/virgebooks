"""VirgeBooks - an AI story book generator to help young children learn to read.

Generates simple stories built from three-letter CVC (consonant-vowel-consonant)
words. Each page has one sentence and one picture, all pictures sharing the same
illustration style. Stories are saved to disk so they can be re-read any time.

Run:
    pip install -r requirements.txt
    export GEMINI_API_KEY=your_key_here     # Windows: set GEMINI_API_KEY=...
    python app.py
Then open http://localhost:5000 in any browser.
"""

import base64
import concurrent.futures
import json
import os
import random
import re
import subprocess
import threading
import time
import tempfile
import uuid

from flask import Flask, jsonify, request, send_from_directory

try:
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover - surfaced clearly at runtime
    genai = None
    types = None

# --- Configuration ---------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORIES_DIR = os.path.join(BASE_DIR, "stories")
STATIC_DIR = os.path.join(BASE_DIR, "static")


def _load_dotenv():
    """Minimal .env loader so users can keep their key in a file (no extra deps)."""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

TEXT_PROVIDER = os.environ.get("VB_TEXT_PROVIDER", "gemini").strip().lower()
IMAGE_PROVIDER = os.environ.get("VB_IMAGE_PROVIDER", "gemini").strip().lower()

TEXT_MODEL = os.environ.get("VB_TEXT_MODEL", "gemini-2.5-flash")
IMAGE_MODEL = os.environ.get("VB_IMAGE_MODEL", "gemini-2.5-flash-image")

# Codex CLI lets a logged-in Codex/ChatGPT subscription create the story text
# and SVG illustrations without needing a separate Gemini or OpenAI API key.
CODEX_COMMAND = os.environ.get("VB_CODEX_COMMAND", "codex")
CODEX_MODEL = os.environ.get("VB_CODEX_MODEL", "").strip()
CODEX_TIMEOUT = int(os.environ.get("VB_CODEX_TIMEOUT", "300"))

DEFAULT_PAGE_COUNT = 10

# How many page illustrations to generate at once. Pages 2..N (and the closing
# page) all reference page 1, so they're independent of each other and can run
# concurrently. Rate-limited (429) requests retry with backoff, so this can run
# hot; tune down if you're on a low-quota tier and want to avoid the retries.
MAX_IMAGE_WORKERS = int(
    os.environ.get("VB_IMAGE_WORKERS", "2" if IMAGE_PROVIDER == "codex" else "10")
)

# Retry budget for rate-limited (429 / quota) image requests, with exponential
# backoff. A throttled request waits and retries instead of dropping a picture.
RATE_LIMIT_MAX_RETRIES = 5

# One fixed style so every picture in every book looks like it belongs together.
ILLUSTRATION_STYLE = (
    "Soft, friendly children's picture-book illustration. Flat vector art with "
    "gentle rounded shapes, thick clean outlines, warm cheerful pastel colors, "
    "and a simple plain background. Cute, wholesome, and easy for a toddler to "
    "read. IMPORTANT: do not put any letters, words, numbers, or text in the "
    "image."
)

# Example word banks for the reading levels below. A random sample is drawn for
# each generation so the model doesn't fixate on the same few words (early list
# items like "cup" used to star in far too many stories).
CVC_WORDS = [
    "cat", "dog", "sun", "hat", "mat", "sat", "run", "big", "red", "pig",
    "bug", "cup", "bed", "hen", "fox", "box", "log", "mud", "jam", "hop",
    "top", "wet", "pot", "pup", "bun", "net", "dig", "hug", "kid", "lap",
    "map", "nap", "pan", "rat", "tap", "van", "web", "yes", "zip", "bat",
    "cab", "cob", "dot", "fan", "fig", "gum", "ham", "jet", "leg", "lid",
    "mop", "nut", "pin", "rug", "sip", "tub", "wig", "yak", "zag", "bee",
]
FOUR_LETTER_WORDS = [
    "stop", "frog", "jump", "hand", "fish", "ship", "duck", "sock", "milk",
    "nest", "hill", "bump", "gift", "sand", "tent", "lamp", "desk", "ring",
    "song", "wind", "fast", "best", "soft", "swim", "clap", "drum", "flag",
    "grin", "help", "kick", "land", "mask", "pond", "rock", "skip", "spin",
    "star", "twig", "wave", "wish",
]
# Words that practice the sh / ch / th / wh sounds, four and five letters long.
DIGRAPH_WORDS = [
    "ship", "shop", "shed", "shut", "fish", "dish", "wish", "rush", "shell",
    "shelf", "brush", "chin", "chip", "chop", "chat", "much", "rich", "chick",
    "lunch", "bench", "thin", "then", "this", "that", "them", "with", "bath",
    "path", "moth", "math", "thump", "when", "whip", "whiz", "shake", "shine",
    "sheep", "teeth", "three", "chase", "cheek", "white", "whale", "beach",
    "peach", "shade",
]
# Long-vowel words: silent e (cake, bike) and vowel teams (rain, feet, boat).
LONG_VOWEL_WORDS = [
    "cake", "lake", "game", "gate", "bike", "kite", "ride", "five", "home",
    "rope", "nose", "bone", "cute", "tube", "mule", "rain", "tail", "sail",
    "snail", "train", "play", "day", "tray", "feet", "seed", "tree", "sleep",
    "green", "leaf", "sea", "team", "read", "boat", "road", "goat", "soap",
    "toad", "smile", "plane", "stone", "snake",
]

# Reading levels (difficulty). Each level swaps the word-building rules in the
# story prompt; everything else (sentence length, joining words, tone) stays the
# same. Add a new entry here to introduce another difficulty. The dict key is the
# value sent by the UI / API; `label` is the human-facing name; `word_rules` is
# the rule line(s) injected into STORY_PROMPT, with {cvc} and {four} filled from
# a fresh random sample of the word banks above at generation time ({digraph}
# and {long} likewise).
LEVELS = {
    "cvc": {
        "label": "Three-letter words (CVC)",
        "word_rules": (
            "- Build sentences almost entirely from simple three-letter CVC words\n"
            "  (consonant-vowel-consonant). Examples: {cvc}."
        ),
    },
    "cvc-plus": {
        "label": "Mostly CVC + some four-letter words",
        "word_rules": (
            "- Build sentences mostly from simple three-letter CVC words\n"
            "  (consonant-vowel-consonant). Examples: {cvc}.\n"
            "- Also use SOME easy four-letter words, but sparingly: at most one per\n"
            "  sentence, on roughly a third of the pages, and only sound-it-out simple\n"
            "  ones a beginner can blend. Examples: {four}."
        ),
    },
    "four-plus": {
        "label": "Plenty of four-letter words",
        "word_rules": (
            "- Build sentences from a friendly mix of simple three-letter CVC words\n"
            "  (consonant-vowel-consonant) and easy four-letter words.\n"
            "- Every sentence MUST contain at least one four-letter word, and a sentence\n"
            "  may have several. This is the most important rule.\n"
            "- Keep every word a simple sound-it-out one a beginner can blend -- no silent\n"
            "  letters or tricky spellings.\n"
            "  Three-letter examples: {cvc}.\n"
            "  Four-letter examples: {four}."
        ),
    },
    "sounds": {
        "label": "Longer words: sh, ch, th + long vowels",
        "word_rules": (
            "- Build sentences mostly from four-letter words, with some five-letter\n"
            "  words. Short three-letter words are fine to fill in around them.\n"
            "- Every sentence MUST contain at least one word with sh, ch, th, or wh\n"
            "  (the word \"the\" does not count). This is the most important rule.\n"
            "  Use a mix of all four sounds across the story, not just one.\n"
            "  Examples: {digraph}.\n"
            "- On at least a third of the pages, also use a long vowel word: a silent-e\n"
            "  word (cake, bike, home, cute) or a vowel team (rain, play, feet, sea, boat).\n"
            "  Examples: {long}.\n"
            "- Keep every word easy to sound out. Silent e and the vowel teams above are\n"
            "  fine, but avoid other tricky spellings (no kn, gh, ough, or odd vowels).\n"
            "  Other four-letter examples: {four}."
        ),
    },
}

# The level used when none is requested or an unknown one is sent.
DEFAULT_LEVEL = "cvc"

STORY_PROMPT = """You write tiny stories for a young child who is just starting to learn to read.

Follow these rules EXACTLY:
{word_rules}
- You MAY use these little joining words, but use them sparingly: a, the, is, in,
  on, it, and, to.
- Every page is ONE short sentence of 3 to 6 words.
- Start each sentence with a capital letter and end it with a period.
- Keep a single cute main character (an animal or kid) across all pages so the
  pictures can stay consistent.
- Make it a fun, gentle little story a toddler will enjoy.
- The example words are only examples, NOT a required list. Use a wide variety
  of simple words that fit the rules, and do not build the story around the
  same everyday objects every time.

Write a story of exactly {page_count} pages.{extra}

Return ONLY valid JSON in this exact shape:
{{
  "title": "a short title using simple words",
  "character": "a one-line description of the main character so an illustrator can draw the same character every time (e.g. 'a small round orange cat with a red collar')",
  "pages": [
    {{"sentence": "The cat sat.", "picture": "a short description of what to draw for this sentence"}}
  ]
}}
"""


def normalize_level(level):
    """Map a requested level to a known key, falling back to the default."""
    key = (level or "").strip().lower().replace("_", "-")
    return key if key in LEVELS else DEFAULT_LEVEL


def build_story_prompt(page_count, instructions, level, rng=random):
    """Render STORY_PROMPT for the given page count, instructions, and level.

    Example words are freshly sampled and shuffled each call so the model
    doesn't latch onto the same handful of words (looking at you, "cup").
    """
    extra = ""
    if instructions and instructions.strip():
        extra = (
            "\n\nThe grown-up asked for this kind of story (keep following all the "
            "rules above): " + instructions.strip() + "\n"
            "Exception: if the request names a thing whose word is longer than the "
            "word rules allow (like 'dragon' or 'tiger'), you may still use that "
            "exact word -- the child wants to see it. Use it naturally where the "
            "story needs it, and keep every OTHER word within the rules."
        )
    word_rules = LEVELS[normalize_level(level)]["word_rules"].format(
        cvc=", ".join(rng.sample(CVC_WORDS, 24)),
        four=", ".join(rng.sample(FOUR_LETTER_WORDS, 20)),
        digraph=", ".join(rng.sample(DIGRAPH_WORDS, 20)),
        long=", ".join(rng.sample(LONG_VOWEL_WORDS, 16)),
    )
    return STORY_PROMPT.format(
        page_count=page_count, extra=extra, word_rules=word_rules
    )

app = Flask(__name__, static_folder=None)

# Track in-progress generations so the UI can poll for status.
_jobs = {}
_jobs_lock = threading.Lock()


# --- Helpers ---------------------------------------------------------------

def get_client():
    """Return a Gemini client, or raise a clear error if not configured."""
    if genai is None:
        raise RuntimeError(
            "The google-genai package is not installed. Run: "
            "pip install -r requirements.txt"
        )
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No API key found. Set the GEMINI_API_KEY environment variable to "
            "your Google Gemini API key, then restart the app."
        )
    return genai.Client(api_key=api_key)


def run_codex(prompt, sandbox="read-only", images=None):
    """Run Codex CLI with the user's logged-in subscription and return its answer."""
    with tempfile.NamedTemporaryFile("r", encoding="utf-8", delete=False) as out:
        out_path = out.name
    try:
        cmd = [
            CODEX_COMMAND,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            sandbox,
            "--output-last-message",
            out_path,
        ]
        for image in images or []:
            cmd.extend(["--image", image])
        if CODEX_MODEL:
            cmd.extend(["--model", CODEX_MODEL])
        cmd.append("-")
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=CODEX_TIMEOUT,
            cwd=BASE_DIR,
            check=False,
        )
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                message = f.read().strip()
        except FileNotFoundError:
            message = ""
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or message).strip()
            raise RuntimeError(
                "Codex CLI failed. Make sure `codex` is installed and logged in."
                + (f"\n{detail}" if detail else "")
            )
        return message or proc.stdout.strip()
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Codex CLI was not found. Install it with: npm install -g @openai/codex"
        ) from exc
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def extract_json_object(text):
    """Extract a JSON object from raw model output, including fenced blocks."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def is_png(path):
    """True if a file exists and starts with a PNG signature."""
    if not os.path.exists(path):
        return False
    with open(path, "rb") as f:
        return f.read(8) == b"\x89PNG\r\n\x1a\n"


def slugify(text):
    text = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return text[:40] or "story"


def story_dir(story_id):
    return os.path.join(STORIES_DIR, story_id)


def load_story(story_id):
    path = os.path.join(story_dir(story_id), "story.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_stories():
    out = []
    if not os.path.isdir(STORIES_DIR):
        return out
    for name in os.listdir(STORIES_DIR):
        data = load_story(name)
        if not data:
            continue
        out.append(
            {
                "id": data["id"],
                "title": data.get("title", "A Story"),
                "cover": data.get("cover"),
                "created": data.get("created", 0),
                "page_count": len(data.get("pages", [])),
            }
        )
    out.sort(key=lambda s: s.get("created", 0), reverse=True)
    return out


def _pages_with_word(pages, test):
    """How many pages have at least one sentence word (lowercased) passing test."""
    count = 0
    for page in pages:
        words = re.findall(r"[a-z]+", page.get("sentence", "").lower())
        if any(test(w) for w in words):
            count += 1
    return count


def pages_with_four_letter_word(pages):
    """How many pages have at least one four-letter word in their sentence."""
    return _pages_with_word(pages, lambda w: len(w) == 4)


def _is_digraph_word(word):
    return word != "the" and re.search(r"sh|ch|th|wh", word) is not None


def pages_with_digraph_word(pages):
    """How many pages have a word with sh, ch, th, or wh (not counting "the")."""
    return _pages_with_word(pages, _is_digraph_word)


# Silent-e look-alikes that do not make a long vowel.
_NOT_LONG_VOWEL = {
    "are", "have", "give", "live", "love", "come", "some", "done", "gone",
    "none", "one", "were", "there", "where", "move", "lose",
}


def _is_long_vowel_word(word):
    if word in _NOT_LONG_VOWEL or word[:-1] in _NOT_LONG_VOWEL:
        return False
    # Silent e (cake, ate, takes) or a vowel team (rain, play, feet, sea, boat).
    return bool(
        re.search(r"[aeiou][^aeiouwy]es?$", word)
        or re.search(r"ai|ay|ee|ea|oa", word)
    )


def pages_with_long_vowel_word(pages):
    """How many pages have a silent-e or vowel-team (long vowel) word."""
    return _pages_with_word(pages, _is_long_vowel_word)


def level_score(level, pages):
    """How well pages fit a level; higher is better. Used to pick a retry."""
    if level == "sounds":
        return pages_with_digraph_word(pages) + pages_with_long_vowel_word(pages)
    return pages_with_four_letter_word(pages)


def level_satisfied(level, pages):
    """True if the story's pages actually honor the requested reading level.

    Models sometimes ignore the harder level and write pure CVC anyway, so we
    verify the promise programmatically and let the caller retry.
    """
    if level == "four-plus":
        # The rule says every sentence, but accept half as good enough rather
        # than burning retries chasing perfection.
        return pages_with_four_letter_word(pages) >= max(1, len(pages) // 2)
    if level == "cvc-plus":
        return pages_with_four_letter_word(pages) >= 1
    if level == "sounds":
        # Same leniency as four-plus: digraphs on half the pages, and long
        # vowels on a quarter (the prompt asks for a third).
        return pages_with_digraph_word(pages) >= max(1, len(pages) // 2) and (
            pages_with_long_vowel_word(pages) >= max(1, len(pages) // 4)
        )
    return True


def generate_story_text(client, instructions, page_count, level=DEFAULT_LEVEL):
    level = normalize_level(level)

    def ask():
        # Fresh prompt per attempt so a retry also gets a fresh word sample.
        prompt = build_story_prompt(page_count, instructions, level)

        if TEXT_PROVIDER == "codex":
            data = extract_json_object(
                run_codex(
                    prompt
                    + "\n\nReturn ONLY the JSON object. Do not include markdown, commentary, "
                    "or code fences. Do not run shell commands."
                )
            )
        elif TEXT_PROVIDER == "gemini":
            resp = client.models.generate_content(
                model=TEXT_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=1.0,
                ),
            )
            data = json.loads(resp.text)
        else:
            raise RuntimeError(
                f"Unsupported VB_TEXT_PROVIDER={TEXT_PROVIDER!r}. Use 'gemini' or 'codex'."
            )

        pages = data.get("pages", [])[:page_count]
        if not pages:
            raise RuntimeError("The model did not return any story pages.")
        return data.get("title", "A Story"), data.get("character", ""), pages

    title, character, pages = ask()
    if not level_satisfied(level, pages):
        # One retry; keep whichever attempt honors the level better.
        retry = ask()
        if level_score(level, retry[2]) > level_score(level, pages):
            title, character, pages = retry
    return title, character, pages


def _is_rate_limit_error(exc):
    """True if an exception looks like an API rate-limit / quota (429) error."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return True
    msg = str(exc).lower()
    return any(
        s in msg for s in ("429", "resource_exhausted", "rate limit", "quota")
    )


def generate_image(client, picture_desc, character, out_path, reference_image=None):
    """Generate one illustration. Returns True on success.

    Gemini and Codex both write PNG files. Codex CLI uses Image Gen 2 through
    the user's logged-in Codex subscription, which avoids requiring a separate
    image-generation API key.
    Retries with exponential backoff when Gemini rate-limits us, so running many
    images concurrently doesn't silently drop pictures.
    """
    prompt_parts = [
        ILLUSTRATION_STYLE,
        f"Main character: {character}." if character else "",
        f"Scene to draw: {picture_desc}.",
        "Keep the same character design and the same art style as any reference "
        "image provided.",
    ]

    if IMAGE_PROVIDER == "codex":
        codex_prompt = "\n".join(p for p in prompt_parts if p)
        codex_prompt += (
            "\n\nUse Image Gen 2 to create one normal PNG illustration for a "
            "toddler's storybook. Make it cute, warm, colorful, and gentle; "
            "avoid scary expressions, extra limbs, distorted anatomy, text, "
            "letters, numbers, logos, or watermarks. Use a 4:3 landscape "
            "composition suitable for a picture book page. Save the final PNG "
            f"exactly at this path: {out_path}. Do not create SVG. After saving, "
            "verify the file exists and starts with the PNG signature."
        )
        attachments = []
        if reference_image and os.path.exists(reference_image):
            attachments.append(reference_image)
            codex_prompt += (
                "\n\nThe attached reference image is page 1. Keep the same main "
                "character and overall storybook style, while drawing the new scene."
            )
        run_codex(codex_prompt, sandbox="workspace-write", images=attachments)
        return is_png(out_path)

    if IMAGE_PROVIDER != "gemini":
        raise RuntimeError(
            f"Unsupported VB_IMAGE_PROVIDER={IMAGE_PROVIDER!r}. Use 'gemini' or 'codex'."
        )

    contents = ["\n".join(p for p in prompt_parts if p)]
    if reference_image and os.path.exists(reference_image):
        with open(reference_image, "rb") as f:
            contents.append(
                types.Part.from_bytes(data=f.read(), mime_type="image/png")
            )

    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        try:
            resp = client.models.generate_content(
                model=IMAGE_MODEL, contents=contents
            )
            for part in resp.candidates[0].content.parts:
                inline = getattr(part, "inline_data", None)
                if inline and inline.data:
                    data = inline.data
                    if isinstance(data, str):
                        data = base64.b64decode(data)
                    with open(out_path, "wb") as f:
                        f.write(data)
                    return True
            return False  # responded, but contained no image
        except Exception as exc:
            if attempt < RATE_LIMIT_MAX_RETRIES and _is_rate_limit_error(exc):
                # Exponential backoff with jitter to avoid a retry stampede.
                delay = min(2 ** attempt, 30) + random.uniform(0, 1)
                time.sleep(delay)
                continue
            raise
    return False


def run_generation(job_id, instructions, page_count, level=DEFAULT_LEVEL):
    """Background worker: build the whole story, updating job status as it goes."""

    level = normalize_level(level)

    def set_status(**kw):
        with _jobs_lock:
            _jobs[job_id].update(kw)

    try:
        client = get_client() if "gemini" in (TEXT_PROVIDER, IMAGE_PROVIDER) else None
        set_status(stage="writing", message="Writing the story...")
        title, character, pages = generate_story_text(
            client, instructions, page_count, level
        )

        story_id = f"{int(time.time())}-{slugify(title)}-{job_id[:6]}"
        d = story_dir(story_id)
        os.makedirs(d, exist_ok=True)

        total = len(pages) + 1  # +1 for the closing "The End." page
        set_status(
            stage="drawing", message="Drawing the pictures...", total=total, done=0
        )

        done_count = [0]
        done_lock = threading.Lock()

        def draw(picture_desc, img_name, reference):
            """Generate one illustration; bump progress. Returns the name or None."""
            img_path = os.path.join(d, img_name)
            try:
                ok = generate_image(client, picture_desc, character, img_path, reference)
            except Exception:
                ok = False
            with done_lock:
                done_count[0] += 1
                set_status(done=done_count[0])
            return img_name if ok else None

        # 1) Draw page 1 first as the style/character anchor for everything else.
        image_ext = "png"

        anchor_name = draw(
            pages[0].get("picture", pages[0].get("sentence", "")),
            f"page_1.{image_ext}",
            None,
        )
        anchor = os.path.join(d, anchor_name) if anchor_name else None
        images = [anchor_name] + [None] * (len(pages) - 1)
        end_image = [None]

        # 2) Fan out the rest of the pages + the end page, all against the anchor.
        def schedule():
            for i in range(1, len(pages)):
                desc = pages[i].get("picture", pages[i].get("sentence", ""))
                yield ("page", i, desc, f"page_{i + 1}.{image_ext}")
            yield (
                "end",
                None,
                "the main character smiling and waving goodbye to the reader, a "
                "warm and cozy story-ending scene",
                f"page_end.{image_ext}",
            )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_IMAGE_WORKERS
        ) as pool:
            futures = {
                pool.submit(draw, desc, name, anchor): (kind, idx)
                for kind, idx, desc, name in schedule()
            }
            for fut in concurrent.futures.as_completed(futures):
                kind, idx = futures[fut]
                result = fut.result()
                if kind == "page":
                    images[idx] = result
                else:
                    end_image[0] = result

        out_pages = [
            {"sentence": pages[i].get("sentence", "").strip(), "image": images[i]}
            for i in range(len(pages))
        ]
        out_pages.append({"sentence": "The End.", "image": end_image[0]})

        cover = next((p["image"] for p in out_pages if p["image"]), None)
        story = {
            "id": story_id,
            "title": title,
            "character": character,
            "level": level,
            "created": int(time.time()),
            "cover": cover,
            "pages": out_pages,
        }
        with open(os.path.join(d, "story.json"), "w", encoding="utf-8") as f:
            json.dump(story, f, indent=2)

        set_status(stage="done", message="Ready!", story_id=story_id)
    except Exception as e:  # surface the real reason to the UI
        set_status(stage="error", message=str(e))


# --- Routes ----------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/api/levels")
def api_levels():
    """Reading levels (difficulty) the UI can offer, in order, with the default."""
    return jsonify(
        {
            "default": DEFAULT_LEVEL,
            "levels": [{"id": k, "label": v["label"]} for k, v in LEVELS.items()],
        }
    )


@app.route("/api/stories")
def api_list_stories():
    return jsonify(list_stories())


@app.route("/api/stories/<story_id>")
def api_get_story(story_id):
    data = load_story(story_id)
    if not data:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/stories/<story_id>/<path:filename>")
def story_image(story_id, filename):
    return send_from_directory(story_dir(story_id), filename)


@app.route("/api/generate", methods=["POST"])
def api_generate():
    body = request.get_json(silent=True) or {}
    instructions = body.get("instructions", "")
    page_count = int(body.get("pages") or DEFAULT_PAGE_COUNT)
    page_count = max(3, min(page_count, 12))
    level = normalize_level(body.get("level"))

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"stage": "starting", "message": "Getting ready..."}
    threading.Thread(
        target=run_generation,
        args=(job_id, instructions, page_count, level),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/api/jobs/<job_id>")
def api_job_status(job_id):
    with _jobs_lock:
        status = _jobs.get(job_id)
    if not status:
        return jsonify({"error": "not found"}), 404
    return jsonify(status)


if __name__ == "__main__":
    os.makedirs(STORIES_DIR, exist_ok=True)
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  VirgeBooks is running!  Open http://localhost:{port} in your browser.\n")
    app.run(host="0.0.0.0", port=port, debug=False)
