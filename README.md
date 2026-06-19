# 📚 VirgeBooks

A tiny AI story-book generator to help a young child learn to read.

Stories are built from simple words, with a **reading level** you choose per story so
the difficulty can grow with your reader:

- **Three-letter words (CVC)** — the default; everything is consonant-vowel-consonant,
  like *cat, dog, sun, hat*.
- **Mostly CVC + some four-letter words** — still mostly CVC, but sprinkles in a few
  easy, sound-it-out four-letter words like *frog, jump, fish, milk* for variety once a
  reader is ready for them.

Each page shows **one short sentence and one picture**, and every picture in every book
shares the same friendly illustration style. Stories are saved so they can be re-read
any time — the home screen is a shelf of past stories plus a box to make a new one.

It runs as a small local web app, so it works on **any platform** (Windows, Mac, Linux,
and tablets/phones on the same network) through a web browser.

## Setup

1. **Pick an AI backend.**
   - Gemini API key backend: get a free-tier key at https://aistudio.google.com/apikey
   - Codex CLI backend: install/login once with your Codex subscription:
     ```bash
     npm install -g @openai/codex
     codex login
     ```
2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
3. **Configure your backend.** Either copy `.env.example` to `.env` and edit it, or set
   variables in your shell.

   Gemini text + Gemini PNG pictures:
   ```bash
   export VB_TEXT_PROVIDER=gemini
   export VB_IMAGE_PROVIDER=gemini
   export GEMINI_API_KEY=your_key_here        # macOS / Linux
   set GEMINI_API_KEY=your_key_here           # Windows (cmd)
   ```

   Codex text + Codex PNG pictures, using your logged-in Codex subscription:
   ```bash
   export VB_TEXT_PROVIDER=codex
   export VB_IMAGE_PROVIDER=codex
   ```
4. **Run it:**
   ```bash
   python app.py
   ```
5. Open **http://localhost:5000** in any browser.

   To read on a tablet/phone, open `http://<your-computer-ip>:5000` on a device on the
   same Wi-Fi.

## Using it

- **Make a story:** Optionally type what it should be about (e.g. *"a pig and a red hat"*),
  pick a **reading level** and how many pages, and tap **Make a story**. Leave the box
  empty for a surprise.
- **Read a story:** Tap a book on the shelf. Tap the **left or right half of the picture**
  (or the big arrows / keyboard arrows) to turn pages — one sentence at a time.

## How it works

- `VB_TEXT_PROVIDER=gemini` uses `gemini-2.5-flash` to write the story as structured JSON
  (title, character, one sentence per page).
- `VB_IMAGE_PROVIDER=gemini` uses `gemini-2.5-flash-image` (Nano Banana) to draw PNG pages.
- `VB_TEXT_PROVIDER=codex` and `VB_IMAGE_PROVIDER=codex` use the local `codex` CLI and your
  logged-in Codex subscription. Story text is generated as JSON, and pictures are generated
  as PNG illustrations with Image Gen 2, so no separate image API key is required.
- A fixed style prompt plus the first page used as a visual reference keeps the art and
  character consistent.
- **Pictures are drawn concurrently.** Page 1 is drawn first as the style/character
  anchor, then the remaining pages and the "The End." page are generated in parallel —
  all referencing that same anchor, so they stay consistent while finishing several
  times faster (~4x in practice).
- Each story is saved under `stories/<id>/` as `story.json` plus `page_N.png` images.

## Notes

- **Reading levels** are defined in the `LEVELS` dict near the top of `app.py`. Each
  level just swaps the word-building rules in the story prompt (sentence length, tone,
  and everything else stay the same), so adding a new difficulty is a few lines — give it
  an `id`, a `label`, and its `word_rules`. The UI fills its dropdown from `GET /api/levels`,
  and `POST /api/generate` accepts a `level` field (unknown/missing levels fall back to
  `DEFAULT_LEVEL`, currently `cvc`). The chosen level is saved in each story's `story.json`.
- Providers and models are configurable with environment variables (`VB_TEXT_PROVIDER`,
  `VB_IMAGE_PROVIDER`, `VB_TEXT_MODEL`, `VB_IMAGE_MODEL`, `VB_CODEX_MODEL`), and the
  illustration style is configurable near the top of `app.py` (`ILLUSTRATION_STYLE`).
- Image concurrency defaults to 10 parallel draws (`VB_IMAGE_WORKERS`). Rate-limited
  (429) requests retry automatically with exponential backoff, so a throttled draw waits
  and retries instead of dropping a picture. Lower the worker count if you're on a
  low-quota tier and want to avoid the backoff waits.
- Codex Image Gen 2 generation defaults to 2 parallel draws because each picture is a separate
  Codex CLI run. Increase `VB_IMAGE_WORKERS` if you want more speed.
- If image generation fails for a page, the story still saves and shows the sentence
  without a picture.
