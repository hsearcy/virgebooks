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
import json
import os
import re
import threading
import time
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

TEXT_MODEL = "gemini-2.5-flash"
IMAGE_MODEL = "gemini-2.5-flash-image"

DEFAULT_PAGE_COUNT = 10

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
    resp = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=1.0,
        ),
    )
    data = json.loads(resp.text)
    pages = data.get("pages", [])[:page_count]
    if not pages:
        raise RuntimeError("The model did not return any story pages.")
    return data.get("title", "A Story"), data.get("character", ""), pages


def generate_image(client, picture_desc, character, out_path, reference_png=None):
    """Generate one illustration. Returns True on success."""
    prompt_parts = [
        ILLUSTRATION_STYLE,
        f"Main character: {character}." if character else "",
        f"Scene to draw: {picture_desc}.",
        "Keep the same character design and the same art style as any reference "
        "image provided.",
    ]
    contents = ["\n".join(p for p in prompt_parts if p)]
    if reference_png and os.path.exists(reference_png):
        with open(reference_png, "rb") as f:
            contents.append(
                types.Part.from_bytes(data=f.read(), mime_type="image/png")
            )
    resp = client.models.generate_content(model=IMAGE_MODEL, contents=contents)
    for part in resp.candidates[0].content.parts:
        inline = getattr(part, "inline_data", None)
        if inline and inline.data:
            data = inline.data
            if isinstance(data, str):
                data = base64.b64decode(data)
            with open(out_path, "wb") as f:
                f.write(data)
            return True
    return False


def run_generation(job_id, instructions, page_count):
    """Background worker: build the whole story, updating job status as it goes."""

    def set_status(**kw):
        with _jobs_lock:
            _jobs[job_id].update(kw)

    try:
        client = get_client()
        set_status(stage="writing", message="Writing the story...")
        title, character, pages = generate_story_text(
            client, instructions, page_count
        )

        story_id = f"{int(time.time())}-{slugify(title)}-{job_id[:6]}"
        d = story_dir(story_id)
        os.makedirs(d, exist_ok=True)

        set_status(
            stage="drawing",
            message="Drawing the pictures...",
            total=len(pages) + 1,  # +1 for the closing "The End." page
            done=0,
        )

        first_image_path = None
        out_pages = []
        for i, page in enumerate(pages):
            img_name = f"page_{i + 1}.png"
            img_path = os.path.join(d, img_name)
            try:
                ok = generate_image(
                    client,
                    page.get("picture", page.get("sentence", "")),
                    character,
                    img_path,
                    reference_png=first_image_path,
                )
            except Exception:
                ok = False
            if ok and first_image_path is None:
                first_image_path = img_path
            out_pages.append(
                {
                    "sentence": page.get("sentence", "").strip(),
                    "image": img_name if ok else None,
                }
            )
            set_status(done=i + 1)

        # Every book ends with an illustrated "The End." page.
        end_name = "page_end.png"
        end_path = os.path.join(d, end_name)
        try:
            ok = generate_image(
                client,
                "the main character smiling and waving goodbye to the reader, a "
                "warm and cozy story-ending scene",
                character,
                end_path,
                reference_png=first_image_path,
            )
        except Exception:
            ok = False
        out_pages.append({"sentence": "The End.", "image": end_name if ok else None})
        set_status(done=len(pages) + 1)

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
