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

STORY_PROMPT = """You write tiny stories for a 3-year-old who is just starting to learn to read.

Follow these rules EXACTLY:
- Build sentences almost entirely from simple three-letter CVC words
  (consonant-vowel-consonant). Examples: cat, dog, sun, hat, mat, sat, run, big,
  red, pig, bug, cup, bed, hen, fox, box, log, mud, jam, hop, top, wet, pot, pup,
  bun, net, dig, hug, kid, lap, map, nap, pan, rat, tap, van, web, yes, zip.
- You MAY use these little joining words, but use them sparingly: a, the, is, in,
  on, it, and, to.
- Every page is ONE short sentence of 3 to 6 words.
- Start each sentence with a capital letter and end it with a period.
- Keep a single cute main character (an animal or kid) across all pages so the
  pictures can stay consistent.
- Make it a fun, gentle little story a toddler will enjoy.

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


def generate_story_text(client, instructions, page_count):
    extra = ""
    if instructions and instructions.strip():
        extra = (
            "\n\nThe grown-up asked for this kind of story (keep following all the "
            "rules above): " + instructions.strip()
        )
    prompt = STORY_PROMPT.format(page_count=page_count, extra=extra)

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


def run_generation(job_id, instructions, page_count):
    """Background worker: build the whole story, updating job status as it goes."""

    def set_status(**kw):
        with _jobs_lock:
            _jobs[job_id].update(kw)

    try:
        client = get_client() if "gemini" in (TEXT_PROVIDER, IMAGE_PROVIDER) else None
        set_status(stage="writing", message="Writing the story...")
        title, character, pages = generate_story_text(
            client, instructions, page_count
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

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"stage": "starting", "message": "Getting ready..."}
    threading.Thread(
        target=run_generation,
        args=(job_id, instructions, page_count),
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
