// VirgeBooks front-end: a library of stories + a one-page-at-a-time book reader.

const $ = (sel) => document.querySelector(sel);

const views = { library: $("#library"), reader: $("#reader") };
function show(name) {
  for (const [key, el] of Object.entries(views)) el.classList.toggle("hidden", key !== name);
}

// ---------- Library ----------

async function loadShelf() {
  const shelf = $("#shelf");
  let stories = [];
  try {
    stories = await (await fetch("/api/stories")).json();
  } catch (e) {
    stories = [];
  }
  shelf.innerHTML = "";
  if (!stories.length) {
    shelf.innerHTML = '<p class="empty">No stories yet. Make your first one above!</p>';
    return;
  }
  for (const s of stories) {
    const card = document.createElement("button");
    card.className = "book-card";
    const cover = s.cover
      ? `<img class="cover" src="/stories/${s.id}/${s.cover}" alt="">`
      : `<div class="cover placeholder">📖</div>`;
    card.innerHTML = `${cover}<div class="name">${escapeHtml(s.title)}</div>`;
    card.addEventListener("click", () => openBook(s.id));
    shelf.appendChild(card);
  }
}

function escapeHtml(str) {
  const d = document.createElement("div");
  d.textContent = str || "";
  return d.innerHTML;
}

// ---------- Generate a new story ----------

const overlay = $("#overlay");
const overlayMsg = $("#overlay-msg");
const progressWrap = $("#progress-wrap");
const progressBar = $("#progress-bar");

$("#generate-btn").addEventListener("click", generate);

async function generate() {
  const instructions = $("#instructions").value;
  const pages = parseInt($("#page-count").value, 10);
  $("#generate-btn").disabled = true;
  overlay.classList.remove("hidden");
  overlayMsg.textContent = "Getting ready...";
  progressWrap.classList.add("hidden");
  progressBar.style.width = "0%";

  try {
    const res = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instructions, pages }),
    });
    const { job_id } = await res.json();
    await pollJob(job_id);
  } catch (e) {
    overlayMsg.textContent = "Something went wrong. Please try again.";
    await wait(2500);
  } finally {
    overlay.classList.add("hidden");
    $("#generate-btn").disabled = false;
  }
}

async function pollJob(jobId) {
  while (true) {
    await wait(1200);
    let job;
    try {
      job = await (await fetch(`/api/jobs/${jobId}`)).json();
    } catch (e) {
      continue;
    }
    overlayMsg.textContent = job.message || "Working...";
    if (job.stage === "drawing" && job.total) {
      progressWrap.classList.remove("hidden");
      const pct = Math.round((100 * (job.done || 0)) / job.total);
      progressBar.style.width = pct + "%";
    }
    if (job.stage === "done") {
      $("#instructions").value = "";
      await loadShelf();
      openBook(job.story_id);
      return;
    }
    if (job.stage === "error") {
      overlayMsg.textContent = job.message || "Something went wrong.";
      await wait(3500);
      return;
    }
  }
}

// ---------- Book reader ----------

let currentStory = null;
let pageIndex = 0;

async function openBook(storyId) {
  try {
    currentStory = await (await fetch(`/api/stories/${storyId}`)).json();
  } catch (e) {
    return;
  }
  pageIndex = 0;
  buildDots();
  show("reader");
  renderPage();
}

function renderPage() {
  if (!currentStory) return;
  const page = currentStory.pages[pageIndex];
  const img = $("#page-image");
  if (page.image) {
    img.src = `/stories/${currentStory.id}/${page.image}`;
    img.style.visibility = "visible";
  } else {
    img.removeAttribute("src");
    img.style.visibility = "hidden";
  }
  $("#page-sentence").textContent = page.sentence;
  $("#prev").disabled = pageIndex === 0;
  $("#next").disabled = pageIndex === currentStory.pages.length - 1;
  document.querySelectorAll(".dot").forEach((d, i) =>
    d.classList.toggle("active", i === pageIndex)
  );
}

function buildDots() {
  const dots = $("#page-dots");
  dots.innerHTML = "";
  currentStory.pages.forEach(() => {
    const d = document.createElement("span");
    d.className = "dot";
    dots.appendChild(d);
  });
}

function turn(delta) {
  const next = pageIndex + delta;
  if (next < 0 || next >= currentStory.pages.length) return;
  pageIndex = next;
  renderPage();
}

$("#next").addEventListener("click", () => turn(1));
$("#prev").addEventListener("click", () => turn(-1));
$("#close-book").addEventListener("click", () => {
  currentStory = null;
  show("library");
});

// Tap left/right half of the picture to turn pages (toddler-friendly).
$("#page-image").parentElement.addEventListener("click", (e) => {
  const rect = e.currentTarget.getBoundingClientRect();
  turn(e.clientX - rect.left < rect.width / 2 ? -1 : 1);
});

document.addEventListener("keydown", (e) => {
  if (views.reader.classList.contains("hidden")) return;
  if (e.key === "ArrowRight") turn(1);
  if (e.key === "ArrowLeft") turn(-1);
  if (e.key === "Escape") $("#close-book").click();
});

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- Boot ----------
loadShelf();
