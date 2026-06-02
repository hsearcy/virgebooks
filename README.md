# 📚 VirgeBooks

A tiny AI story-book generator to help a young child learn to read.

Every story is built from simple **three-letter CVC words** (consonant-vowel-consonant,
like *cat, dog, sun, hat*). Each page shows **one short sentence and one picture**, and
every picture in every book shares the same friendly illustration style. Stories are
saved so they can be re-read any time — the home screen is a shelf of past stories plus
a box to make a new one.

It runs as a small local web app, so it works on **any platform** (Windows, Mac, Linux,
and tablets/phones on the same network) through a web browser.

## Setup

1. **Get a Gemini API key** (free tier available): https://aistudio.google.com/apikey
2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
3. **Add your key.** Either copy `.env.example` to `.env` and paste your key in, or set
   it in your shell:
   ```bash
   export GEMINI_API_KEY=your_key_here        # macOS / Linux
   set GEMINI_API_KEY=your_key_here           # Windows (cmd)
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
  pick how many pages, and tap **Make a story**. Leave the box empty for a surprise.
- **Read a story:** Tap a book on the shelf. Tap the **left or right half of the picture**
  (or the big arrows / keyboard arrows) to turn pages — one sentence at a time.

## How it works

- `gemini-2.5-flash` writes the story as structured JSON (title, character, one sentence
  per page).
- `gemini-2.5-flash-image` (Nano Banana) draws each page. A fixed style prompt plus the
  first page used as a visual reference keeps the art and character consistent.
- **Pictures are drawn concurrently.** Page 1 is drawn first as the style/character
  anchor, then the remaining pages and the "The End." page are generated in parallel —
  all referencing that same anchor, so they stay consistent while finishing several
  times faster (~4x in practice).
- Each story is saved under `stories/<id>/` as `story.json` plus `page_N.png` images.

## Notes

- Models are configurable near the top of `app.py` (`TEXT_MODEL`, `IMAGE_MODEL`), as is
  the illustration style (`ILLUSTRATION_STYLE`).
- Image concurrency defaults to 10 parallel draws (`VB_IMAGE_WORKERS`). Rate-limited
  (429) requests retry automatically with exponential backoff, so a throttled draw waits
  and retries instead of dropping a picture. Lower the worker count if you're on a
  low-quota tier and want to avoid the backoff waits.
- If image generation fails for a page, the story still saves and shows the sentence
  without a picture.
