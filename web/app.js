// Desireerr's single-page app: plain ES modules, no build step.

// ---------------------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------------------

class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
const raw = (s) => new Raw(s);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function esc(v) {
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(esc).join("");
  if (v === null || v === undefined || v === false) return "";
  return String(v).replace(/[&<>"']/g, (c) => ESC[c]);
}
function html(strings, ...vals) {
  let out = strings[0];
  vals.forEach((v, i) => { out += esc(v) + strings[i + 1]; });
  return raw(out);
}
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem("desireerr." + key);
    localStorage.setItem("desireerr." + key, value);
  } catch { return null; }
}

class HttpError extends Error { constructor(status, msg) { super(msg); this.status = status; } }

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch("/api/" + path, {
    method,
    credentials: "same-origin",
    headers: { "X-Requested-With": "desireerr", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && !["login", "status", "setup", "password"].includes(path.split("?")[0])) {
    me = null;
    go("/login", true);
    throw new HttpError(401, data.error || "Sign in first.");
  }
  if (!res.ok) throw new HttpError(res.status, data.error || `The server answered ${res.status}.`);
  return data;
}

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || v === false) continue;
    p.set(k, Array.isArray(v) ? v.join(",") : String(v));
  }
  const s = p.toString();
  return s ? "?" + s : "";
}

function toast(msg, bad = false) {
  let box = $(".toasts");
  if (!box) { box = document.createElement("div"); box.className = "toasts"; document.body.append(box); }
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : "");
  t.setAttribute("role", "status");
  t.textContent = msg;
  box.append(t);
  setTimeout(() => t.remove(), bad ? 7000 : 3500);
}

// StashDB scales its images with ?size=N (the longer side), so cards fetch small copies and
// only the photo viewer the full image. It only knows these sizes: any other number gets the
// full image, so a wanted size is rounded up to one of them.
const IMAGE_SIZES = [300, 600, 1280];
const imgUrl = (u, size) => {
  const n = size && IMAGE_SIZES.find((s) => s >= size);
  return "/img?u=" + encodeURIComponent(n && !u.includes("?") ? `${u}?size=${n}` : u);
};
// Should an image not come in a smaller size, ask for the full one.
document.addEventListener("error", (e) => {
  const el = e.target;
  if (el.tagName === "IMG" && /%3Fsize%3D\d+/.test(el.src)) el.src = el.src.replace(/%3Fsize%3D\d+/, "");
}, true);
function pickImage(images, landscape = true) {
  if (!images || !images.length) return "";
  const score = (i) => [((i.width || 0) >= (i.height || 0)) === landscape ? 1 : 0, (i.width || 0) * (i.height || 0)];
  return [...images].sort((a, b) => { const x = score(a), y = score(b); return y[0] - x[0] || y[1] - x[1]; })[0].url;
}
function fmtDate(d) {
  if (!d) return "";
  const parts = d.split("-");
  if (parts.length === 1) return d;
  const dt = new Date(d + (parts.length === 2 ? "-01" : "") + "T12:00:00");
  return dt.toLocaleDateString(undefined, parts.length === 2 ? { year: "numeric", month: "short" } : { year: "numeric", month: "short", day: "numeric" });
}
function localToday() {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}
// Whole years from BIRTH to WHEN (or today); both as StashDB gives dates ("1990",
// "1990-05" or "1990-05-03"). "~24" when the dates are too rough to be sure.
function ageAt(birth, when = localToday()) {
  if (!birth || !when) return "";
  const b = birth.split("-").map(Number), w = when.split("-").map(Number);
  const n = Math.min(b.length, w.length);
  let years = w[0] - b[0];
  if (n >= 2 && (w[1] < b[1] || (n === 3 && w[1] === b[1] && w[2] < b[2]))) years--;
  if (!(years >= 0 && years < 120)) return "";
  const sure = n === 3 || (n === 2 && w[1] !== b[1]);
  return (sure ? "" : "~") + years;
}
// A cast member's ages: when the scene was made, and now (or when they died).
function castAges(p, s) {
  const born = p.birth_date, died = p.death_date;
  const made = s.production_date || s.release_date;
  const then = made ? ageAt(born, made) : "", last = ageAt(born, died || undefined);
  const short = !died && then && then === last ? [`${then} then and now`] : [then && `${then} then`,
    died ? `† ${died.slice(0, 4)}${last ? ` at ${last}` : ""}` : last && `${last} now`].filter(Boolean);
  if (!short.length) return "";
  const long = [born && `Born ${fmtDate(born)}`,
    then && `${then} ${s.production_date ? "when this was made" : "at its release"}`,
    died ? `died ${fmtDate(died)}${last ? ` at ${last}` : ""}` : last && `${last} now`].filter(Boolean);
  return html`<small class="ages${died ? " died" : ""}" title="${long.join(" · ")}">${short.join(" · ")}</small>`;
}
function fmtDur(sec) {
  if (!sec) return "";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
function fmtAgo(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} days ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
const titleCase = (s) => (s || "").toLowerCase().replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
const performerName = (p) => p.as || p.performer?.name || "";
// Tag feelings are Desireerr's own (StashDB has none), per user; status carries them.
// A user's feeling for a tag (neutral without one), and how a tag chip shows it. Clicking a
// chip opens it (one at a time): the five feelings to pick from, and a magnifier to the tag's
// page. A middle or Ctrl click still opens the page.
const FEELINGS = [["hate", "Hate", "ban"], ["dislike", "Dislike", "thumbDown"], ["neutral", "Neutral", ""], ["like", "Like", "heart"], ["love", "Love", "heartFill"]];
const feelingOf = (id) => (status.tag_feelings || []).find((t) => t.id === id)?.feeling || "neutral";
const feelingIcon = (f) => FEELINGS.find(([k]) => k === f)[2];
const chipTitle = (f, t) => f === "neutral" ? t.description || "" : `You ${f} this tag`;
const tagChip = (t, extra = "") => {
  const f = feelingOf(t.id), ic = feelingIcon(f);
  return html`<span class="tagchip" data-tag="${t.id}"><a class="chip feel-${f}" href="/tag/${t.id}" title="${chipTitle(f, t)}" data-desc="${t.description || ""}">${ic ? icon(ic) : ""}${t.name}${extra}</a></span>`;
};
let openChip = null;
function closeChip() {
  if (!openChip) return;
  openChip.classList.remove("open");
  $(".chip-tools", openChip)?.remove();
  openChip = null;
}
function openTagChip(w) {
  closeChip();
  const f = feelingOf(w.dataset.tag);
  w.insertAdjacentHTML("beforeend", html`<span class="chip-tools" role="group" aria-label="How you feel about this tag">${FEELINGS.map(([k, label, ic]) => html`<button type="button" data-feel="${k}" class="${k === f ? `on feel-${k}` : ""}" title="${label}" aria-label="${label}" aria-pressed="${k === f}">${icon(ic || "neutral")}</button>`)}<i class="sep"></i><button type="button" data-open title="Open the tag: its scenes" aria-label="Open the tag">${icon("search")}</button></span>`.s);
  w.classList.add("open");
  openChip = w;
}
// Every chip of the tag on the page shows the new feeling; nothing else moves.
function showFeeling(id) {
  const f = feelingOf(id), ic = feelingIcon(f);
  for (const w of $$(`.tagchip[data-tag="${CSS.escape(id)}"]`)) {
    const a = $("a.chip", w);
    a.className = `chip feel-${f}`;
    a.title = chipTitle(f, { description: a.dataset.desc });
    if (a.firstElementChild?.tagName === "svg") a.firstElementChild.remove();
    if (ic) a.insertAdjacentHTML("afterbegin", icon(ic).s);
    for (const b of $$(".chip-tools [data-feel]", w)) {
      const on = b.dataset.feel === f;
      b.className = on ? `on feel-${f}` : "";
      b.setAttribute("aria-pressed", on);
    }
  }
}
document.addEventListener("click", async (e) => {
  const w = e.target.closest(".tagchip");
  if (!w) { closeChip(); return; }
  if (e.target.closest("a.chip")) {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    if (w === openChip) closeChip(); else openTagChip(w);
    return;
  }
  const b = e.target.closest(".chip-tools button");
  if (!b) return;
  e.preventDefault();
  e.stopPropagation();
  const id = w.dataset.tag;
  if (b.hasAttribute("data-open")) { closeChip(); go(`/tag/${id}`); return; }
  if (b.classList.contains("on") || w.classList.contains("busy")) return;
  w.classList.add("busy");
  try {
    const r = await api("tag-feelings", { method: "POST", body: { id, feeling: b.dataset.feel } });
    status.tag_feelings = r.tags;
    status.fav_tags = r.tags.filter((t) => t.feeling === "love" || t.feeling === "like").map(({ id, name }) => ({ id, name }));
    showFeeling(id);
  } catch (err) {
    if (err.status !== 401) toast(err.message, true);
  } finally { w.classList.remove("busy"); }
}, true);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeChip(); });

// ---------------------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------------------

const ICONS = {
  discover: '<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5z"/>',
  film: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 5v14M17 5v14M3 9.5h4M3 14.5h4M17 9.5h4M17 14.5h4"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5M16 4.6a3.5 3.5 0 0 1 0 6.8M18.5 14.8c1.6.8 2.6 2.5 3 5.2"/>',
  building: '<path d="M4 21V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v16M16 9h2a2 2 0 0 1 2 2v10M2 21h20M8 7h4M8 11h4M8 15h4"/>',
  tag: '<path d="M3 12.6V4a1 1 0 0 1 1-1h8.6a1 1 0 0 1 .7.3l7.4 7.4a1 1 0 0 1 0 1.4l-8.6 8.6a1 1 0 0 1-1.4 0l-7.4-7.4a1 1 0 0 1-.3-.7z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
  inbox: '<path d="M3 13h5l1.5 3h5l1.5-3h5"/><path d="M5.5 5h13L21 13v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5z"/>',
  settings: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  logout: '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l5-5-5-5M15 12H4"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6C3.9 8.3 2 12 2 12s3.6 7 10 7a9.6 9.6 0 0 0 5.4-1.6M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  download: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
  heart: '<path d="M12 20.5C6 16.8 3 13.3 3 9.6 3 6.9 5.1 5 7.6 5c1.9 0 3.4 1 4.4 2.6C13 6 14.5 5 16.4 5 18.9 5 21 6.9 21 9.6c0 3.7-3 7.2-9 10.9z"/>',
  neutral: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
  ban: '<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/>',
  thumbDown: '<path d="M17 3v11M17 3h3v11h-3M17 14l-4.2 7a2.4 2.4 0 0 1-2.3-2.4V15H5.3a2 2 0 0 1-2-2.3l1.2-7.6a2 2 0 0 1 2-1.6H17"/>',
  heartFill: '<path fill="currentColor" stroke="none" d="M12 20.5C6 16.8 3 13.3 3 9.6 3 6.9 5.1 5 7.6 5c1.9 0 3.4 1 4.4 2.6C13 6 14.5 5 16.4 5 18.9 5 21 6.9 21 9.6c0 3.7-3 7.2-9 10.9z"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  chevronLeft: '<path d="m15 6-6 6 6 6"/>',
  zoomIn: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5M11 8v6M8 11h6"/>',
  zoomOut: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5M8 11h6"/>',
  photos: '<rect x="3" y="6" width="14" height="14" rx="2"/><path d="M7 3h12a2 2 0 0 1 2 2v12"/><path d="m3 16 4-4 4 4 2-2 4 4"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  bell: '<path d="M6 16v-5a6 6 0 1 1 12 0v5l1.5 2h-15zM10 20.5a2.2 2.2 0 0 0 4 0"/>',
  bellOff: '<path d="M3 3l18 18M8.3 5.3A6 6 0 0 1 18 11v4M6 11v5l-1.5 2H17M10 20.5a2.2 2.2 0 0 0 4 0"/>',
  more: '<circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/>',
  alert: '<path d="M12 3 2 20h20zM12 9v5M12 17h.01"/>',
  play: '<path d="M7 4.5v15l12.5-7.5z"/>',
  bookmark: '<path d="M6 3.5h12v17l-6-4.2-6 4.2z"/>',
  sparkle: '<path fill="currentColor" stroke="none" d="M12 2c.8 5.6 4.4 9.2 10 10-5.6.8-9.2 4.4-10 10-.8-5.6-4.4-9.2-10-10 5.6-.8 9.2-4.4 10-10z"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4.5V11h-6.5"/>',
};
const icon = (name) => raw(`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`);

// ---------------------------------------------------------------------------------------
// State, router, shell
// ---------------------------------------------------------------------------------------

let me = null;
let status = {};
let viewToken = 0;
const root = document.getElementById("app");

const ROUTES = [
  [/^\/$/, viewDiscover, "discover"],
  [/^\/scenes$/, viewScenes, "scenes"],
  [/^\/scene\/([\w-]+)$/, viewScene, "scenes"],
  [/^\/performers$/, viewPerformers, "performers"],
  [/^\/performer\/([\w-]+)$/, viewPerformer, "performers"],
  [/^\/studios$/, viewStudios, "studios"],
  [/^\/studio\/([\w-]+)$/, viewStudio, "studios"],
  [/^\/tags$/, viewTags, "tags"],
  [/^\/tag\/([\w-]+)$/, viewTag, "tags"],
  [/^\/search$/, viewSearch, "search"],
  [/^\/requests$/, viewRequests, "requests"],
  [/^\/saved$/, viewSaved, "saved"],
  [/^\/settings$/, viewSettings, "settings"],
];

function go(path, replace = false) {
  if (replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
  render();
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("a[href]");
  if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  if (a.target === "_blank" || a.origin !== location.origin || a.hasAttribute("download")) return;
  if (a.pathname.startsWith("/static/") || a.pathname === "/img") return;
  e.preventDefault();
  $(".shell")?.classList.remove("drawer");
  if (a.pathname + a.search !== location.pathname + location.search) go(a.pathname + a.search);
  else window.scrollTo(0, 0);
});
window.addEventListener("popstate", () => { popped = true; render(); });

// Back to a list: what "Load more" loaded and where it was scrolled come back as they were,
// without asking StashDB again. Only for back and forward; a link opens a list afresh.
history.scrollRestoration = "manual";
const LIST_MEMORY_TTL = 15 * 60 * 1000;
const LIST_MEMORY_MAX = 20;
const listMemory = new Map();  // URL -> { items, page, total, shown, scroll, at }
let popped = false, currentUrl = "";
const pageUrl = () => location.pathname + location.search;
function rememberList() {  // a list starts afresh at this URL
  currentUrl = pageUrl();
  const m = { items: [], page: 0, total: 0, shown: 0, scroll: 0, at: Date.now() };
  listMemory.delete(currentUrl);
  listMemory.set(currentUrl, m);
  if (listMemory.size > LIST_MEMORY_MAX) listMemory.delete(listMemory.keys().next().value);
  return m;
}
function recalledList(ctx) {
  const m = ctx.back && listMemory.get(pageUrl());
  return m && m.items.length && Date.now() - m.at < LIST_MEMORY_TTL ? m : null;
}
function restoreScroll(y) {
  requestAnimationFrame(() => window.scrollTo(0, y));
}
// A request changes a scene's state on remembered lists too.
function rememberState(sceneId, state) {
  for (const m of listMemory.values()) for (const x of m.items) if (x.id === sceneId) x.state = state;
}

document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) {
    const s = $("#search");
    if (s) { e.preventDefault(); s.focus(); }
  }
});

function setCovers(hidden) {
  document.body.classList.toggle("hide-covers", hidden);
  store("hideCovers", hidden ? "1" : "0");
  const b = $("#covers");
  if (b) {
    b.classList.toggle("on", hidden);
    b.innerHTML = icon(hidden ? "eyeOff" : "eye").s;
    b.title = hidden ? "Show covers" : "Blur covers";
    b.setAttribute("aria-label", b.title);
  }
}

async function refreshStatus() {
  status = await api("status");
  me = status.user;
  updateBadges();
  return status;
}

function updateBadges() {
  const n = status.pending || 0;
  $$("[data-pending]").forEach((el) => { el.textContent = n; el.hidden = !n; });
}

function shell() {
  if ($(".shell")) return $("#view");
  const nav = [
    ["/", "discover", "Discover", "discover"],
    ["/scenes", "film", "Scenes", "scenes"],
    ["/performers", "users", "Performers", "performers"],
    ["/studios", "building", "Studios", "studios"],
    ["/tags", "tag", "Tags", "tags"],
    ["/requests", "inbox", "Requests", "requests"],
    ["/saved", "bookmark", "Saved searches", "saved"],
    ["/settings", "settings", "Settings", "settings"],
  ];
  root.innerHTML = html`
    <div class="shell">
      <aside class="sidebar" aria-label="Main">
        <a class="brand" href="/"><img src="/static/logo.svg" alt="" width="38" height="38"><span class="wordmark">Desir<b>eerr</b></span></a>
        <nav class="nav">
          ${nav.map(([href, ic, label, key]) => html`<a href="${href}" data-nav="${key}">${icon(ic)}<span>${label}</span>${key === "requests" ? html`<span class="count" data-pending hidden></span>` : ""}</a>`)}
        </nav>
        <div class="side-foot">
          <div class="side-user"><span class="avatar">${me.username.slice(0, 1)}</span><div class="who"><div>${me.username}</div><div class="role">${me.is_admin ? "Admin" : me.auto_approve ? "Auto-approved" : "User"}</div></div></div>
          <button class="btn ghost small" id="logout">${icon("logout")} Sign out</button>
        </div>
      </aside>
      <div class="scrim"></div>
      <div class="main">
        <header class="topbar">
          <button class="iconbtn menu-btn" id="menu" aria-label="Menu">${icon("menu")}</button>
          <form class="searchbox" role="search" id="searchform">
            ${icon("search")}
            <input id="search" type="search" placeholder="Search scenes, performers, studios, tags" autocomplete="off" aria-label="Search">
          </form>
          <button class="iconbtn" id="covers" aria-label="Blur covers" title="Blur covers">${icon("eye")}</button>
        </header>
        <main id="view"></main>
      </div>
      <nav class="bottomnav" aria-label="Main">
        <a href="/" data-nav="discover">${icon("discover")}Discover</a>
        <a href="/scenes" data-nav="scenes">${icon("film")}Scenes</a>
        <a href="/tags" data-nav="tags">${icon("tag")}Tags</a>
        <a href="/requests" data-nav="requests">${icon("inbox")}Requests<span class="dot" data-pending hidden></span></a>
        <a href="#" id="more" data-nav="more">${icon("more")}More</a>
      </nav>
    </div>`.s;
  $("#logout").onclick = async () => { await api("logout", { method: "POST" }).catch(() => {}); me = null; go("/login"); };
  $("#menu").onclick = () => $(".shell").classList.toggle("drawer");
  $("#more").onclick = (e) => { e.preventDefault(); $(".shell").classList.toggle("drawer"); };
  $(".scrim").onclick = () => $(".shell").classList.remove("drawer");
  $("#covers").onclick = () => setCovers(!document.body.classList.contains("hide-covers"));
  const search = $("#search");
  const runSearch = (replace) => {
    const q = search.value.trim();
    if (!q) return;
    const target = "/search" + qs({ q });
    if (location.pathname === "/search") go(target, true); else go(target, replace);
  };
  search.addEventListener("input", debounce(() => { if (search.value.trim().length >= 2) runSearch(false); }, 450));
  $("#searchform").onsubmit = (e) => { e.preventDefault(); runSearch(false); search.blur(); };
  setCovers(store("hideCovers") === "1");
  updateBadges();
  return $("#view");
}

async function render() {
  closeLightbox();
  const token = ++viewToken;
  const path = location.pathname;
  const back = popped;
  popped = false;
  const left = listMemory.get(currentUrl);
  if (left && currentUrl !== pageUrl()) left.scroll = window.scrollY;
  currentUrl = pageUrl();
  if (!status.user && !["/login", "/setup"].includes(path)) {
    try { await refreshStatus(); } catch (e) { root.innerHTML = html`<div class="auth"><div class="auth-card"><div class="error-box">${e.message}</div></div></div>`.s; return; }
  }
  if (status.setup_needed && path !== "/setup") return go("/setup", true);
  if (!me && !status.setup_needed && path !== "/login") return go("/login" + qs({ next: path + location.search }), true);
  if (path === "/login") return viewLogin();
  if (path === "/setup") return viewSetup();

  const view = shell();
  let match = null;
  for (const [re, fn, key] of ROUTES) {
    const m = path.match(re);
    if (m) { match = [fn, m.slice(1), key]; break; }
  }
  $$("[data-nav]").forEach((a) => a.classList.toggle("on", !!match && a.dataset.nav === match[2]));
  if (path !== "/search") $("#search").value = "";
  view.innerHTML = html`<div class="center"><div class="spinner"></div></div>`.s;
  window.scrollTo(0, 0);
  if (!match) { view.innerHTML = html`<div class="page"><div class="empty">There's no page here. <a href="/">Go to Discover</a></div></div>`.s; return; }
  const ctx = { token, back, alive: () => token === viewToken };
  try {
    await match[0](view, ...match[1].map(decodeURIComponent), ctx);
  } catch (e) {
    if (!ctx.alive() || e.status === 401) return;
    view.innerHTML = errorPage(e).s;
  }
}

function errorBox(e) {
  const keyMissing = /StashDB API key|refused the API key/.test(e.message);
  return html`<div class="error-box">${e.message}${keyMissing && me?.is_admin ? html` <a href="/settings"><u>Open Settings</u></a>` : ""}</div>`;
}
const errorPage = (e) => html`<div class="page">${errorBox(e)}</div>`;

// ---------------------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------------------

const STATE_LABEL = { available: "Available", processing: "Requested", downloading: "Downloading", pending: "Pending approval", declined: "Declined", failed: "Failed", abandoned: "Given up", none: "Not requested" };
const STATE_ICON = { available: "check", processing: "clock", downloading: "download", pending: "clock", declined: "x", failed: "alert", abandoned: "x" };

function statePill(st) {
  const s = st?.status || "none";
  const label = s === "downloading" ? `Downloading ${st.progress || 0}%` : STATE_LABEL[s];
  return html`<span class="pill ${s}">${STATE_ICON[s] ? icon(STATE_ICON[s]) : ""}${label}</span>`;
}
function stateDot(st) {
  const s = st?.status || "none";
  if (s === "none") return "";
  return html`<span class="state-dot ${s}" title="${s === "downloading" ? `Downloading ${st.progress || 0}%` : STATE_LABEL[s]}">${icon(STATE_ICON[s])}</span>`;
}
const canRequest = (st) => ["none", "declined", "failed", "abandoned"].includes(st?.status || "none");

// Jellyfin's page for a downloaded scene: at the address set in Settings, or on the host
// Desireerr is opened at, on Jellyfin's port.
function watchUrl(st) {
  const j = status.jellyfin;
  if (!j || !st?.jellyfin_id) return "";
  const base = j.link || `${location.protocol}//${location.hostname}:8096`;
  return `${base}/web/#/details?id=${encodeURIComponent(st.jellyfin_id)}&serverId=${encodeURIComponent(j.server_id)}`;
}
const watchButton = (st, cls = "") => {
  const url = watchUrl(st);
  return url ? html`<a class="btn ok ${cls}" href="${url}" target="_blank" rel="noreferrer">${icon("play")}Watch</a>` : "";
};
// The requester and admins can search again for a stalled request, or give up on it.
const mayNudge = (r) => r && r.stalled_days && (me.is_admin || r.user_id === me.id);
const stalledNote = (r) => html`No release found in ${r.stalled_days} days.`;

function sceneQuick(s) {
  return html`${stateDot(s.state)}${canRequest(s.state) ? html`<button class="btn tiny primary req-btn" data-act="request" data-id="${s.id}">${icon("plus")}Request</button>` : ""}`;
}
function progressBar(st) {
  return st?.status === "downloading" ? html`<div class="progress"><i style="width:${st.progress || 0}%"></i></div>` : "";
}
const noImg = (ic = "film") => html`<div class="noimg">${icon(ic)}</div>`;

function sceneCard(s) {
  const im = pickImage(s.images, true);
  const people = (s.performers || []).map(performerName).filter(Boolean).join(", ");
  const meta = [s.studio?.name, fmtDate(s.release_date)].filter(Boolean).join(" · ");
  // The picture opens the photo viewer (at the card's picture), the text the scene.
  const photos = (s.images || []).map((i) => i.url);
  // Rank points, where a row is ranked: in the corner, where Request shows on hover (or
  // beside the state, when there's nothing to request).
  const points = s.points != null ? html`<span class="points" title="Rank points: how well it matches your taste">${icon("sparkle")}${s.points}</span>` : "";
  return html`<div class="card scene-card" data-scene="${s.id}">
    <div class="thumb">
      ${im ? html`<button type="button" class="zoom-card" data-photos="${JSON.stringify(photos)}" data-start="${photos.indexOf(im)}" data-title="${s.title || ""}" aria-label="Show the picture larger">
        <img src="${imgUrl(im, 600)}" alt="" loading="lazy" decoding="async"><span class="magnifier">${icon("zoomIn")}</span></button>` : noImg()}
      ${s.duration ? html`<span class="dur">${fmtDur(s.duration)}</span>` : ""}
      ${points && canRequest(s.state) ? points : ""}
      <div class="quick">${points && !canRequest(s.state) ? points : ""}${sceneQuick(s)}</div>
      ${progressBar(s.state)}
    </div>
    <a class="body" href="/scene/${s.id}">
      <div class="title">${s.title || "Untitled"}</div>
      <div class="meta">${meta}</div>
      ${people ? html`<div class="people-line">${people}</div>` : ""}
    </a>
  </div>`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest(".zoom-card");
  if (b) lightbox(JSON.parse(b.dataset.photos).map((url) => ({ url })), +b.dataset.start, b.dataset.title);
});

function personCard(p) {
  const im = pickImage(p.images, false) || p.image;
  return html`<a class="card person-card" href="/performer/${p.id}">
    <div class="thumb">${im ? html`<img src="${imgUrl(im, 400)}" alt="" loading="lazy" decoding="async">` : noImg("users")}</div>
    ${p.is_favorite || p.followed ? html`<span class="fav" title="${p.followed ? "Followed" : "StashDB favourite"}">${icon("heartFill")}</span>` : ""}
    <div class="name">${p.name}${p.disambiguation ? html`<small>${p.disambiguation}</small>` : p.scene_count ? html`<small>${p.scene_count} scenes</small>` : ""}</div>
  </a>`;
}

function studioCard(s) {
  const im = pickImage(s.images, true) || s.image;
  return html`<a class="card studio-card" href="/studio/${s.id}">
    <div class="thumb">${im ? html`<img src="${imgUrl(im)}" alt="" loading="lazy" decoding="async">` : noImg("building")}</div>
    ${s.is_favorite || s.followed ? html`<span class="fav" title="Favourite">${icon("heartFill")}</span>` : ""}
    <div class="name">${s.name}${s.parent ? html`<small>${s.parent.name}</small>` : ""}</div>
  </a>`;
}

function requestRow(r, { compact = false } = {}) {
  const st = r.state || { status: r.status };
  const acts = [];
  if (me.is_admin && (st.status === "pending" || (r.status === "failed" && canRequest(st)))) {
    acts.push(html`<button class="btn small ok" data-act="approve" data-rid="${r.id}">${icon("check")}${r.status === "failed" ? "Retry" : "Approve"}</button>`);
    acts.push(html`<button class="btn small danger" data-act="decline" data-rid="${r.id}">${icon("x")}Decline</button>`);
  }
  if (st.status === "available") acts.push(watchButton(st, "small"));
  if (mayNudge(r)) {
    acts.push(html`<button class="btn small" data-act="search" data-rid="${r.id}" title="Whisparr searches the indexers again now">${icon("refresh")}Search again</button>`);
    acts.push(html`<button class="btn small ghost" data-act="giveup" data-rid="${r.id}" title="Whisparr stops waiting for a release; it can be requested again later">${icon("x")}Give up</button>`);
  }
  if (r.status === "pending" && (me.is_admin || r.user_id === me.id) && st.status === "pending") {
    acts.push(html`<button class="btn small ghost" data-act="cancel" data-rid="${r.id}">Cancel request</button>`);
  } else if (me.is_admin && ["approved", "declined", "failed", "abandoned"].includes(r.status)) {
    acts.push(html`<button class="btn small ghost" data-act="cancel" data-rid="${r.id}">Remove</button>`);
  }
  return html`<div class="req" data-request="${r.id}">
    ${r.image ? html`<div class="bg"><img src="${imgUrl(r.image, 400)}" alt="" loading="lazy"></div>` : ""}
    <a class="thumb" href="/scene/${r.scene_id}">${r.image ? html`<img src="${imgUrl(r.image, 400)}" alt="" loading="lazy">` : noImg()}${progressBar(st)}</a>
    <div class="info">
      <a class="t" href="/scene/${r.scene_id}">${r.title || "Untitled"}</a>
      <div class="m">${[r.studio, fmtDate(r.release_date)].filter(Boolean).join(" · ")}</div>
      <div>${statePill(st)}</div>
      <div class="m">Requested ${fmtAgo(r.created)}${r.requested_by ? ` by ${r.requested_by}` : ""}${r.via ? ` · saved search “${r.via}”` : ""}${r.decided_by_name && r.status !== "pending" ? ` · ${{ declined: "declined", failed: "tried", abandoned: "given up" }[r.status] || "approved"} by ${r.decided_by_name}` : ""}</div>
      ${r.error && r.status === "failed" ? html`<div class="err">${r.error}</div>` : ""}
      ${r.stalled_days ? html`<div class="m warn-text">${stalledNote(r)}</div>` : ""}
    </div>
    ${compact ? "" : html`<div class="acts">${acts}</div>`}
  </div>`;
}

function slider(title, items, kind, link, cardFn) {
  return html`<section class="section">
    <div class="section-head"><h2>${title}</h2>${link ? html`<a class="more" href="${link}">See all${icon("chevron")}</a>` : ""}</div>
    ${items.length ? html`<div class="slider ${kind}">${items.map(cardFn)}</div>` : html`<div class="empty">Nothing here yet.</div>`}
  </section>`;
}

// Request, approve, decline, cancel, follow: one handler for every page.
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  e.preventDefault();
  e.stopPropagation();
  if (b.classList.contains("busy")) return;
  b.classList.add("busy");
  const act = b.dataset.act;
  try {
    if (act === "request") {
      const r = await api("requests", { method: "POST", body: { scene_id: b.dataset.id } });
      toast(r.status === "approved" ? "Requested. Whisparr is searching for it." : r.status === "pending" ? "Requested. It's waiting for an admin to approve it." : "Request saved.");
      afterChange(r.scene_id, r.state);
    } else if (act === "approve") {
      const r = await api(`requests/${b.dataset.rid}/approve`, { method: "POST" });
      toast("Approved. Whisparr is searching for it.");
      afterChange(r.scene_id, r.state);
    } else if (act === "decline") {
      const r = await api(`requests/${b.dataset.rid}/decline`, { method: "POST" });
      toast("Declined.");
      afterChange(r.scene_id, r.state);
    } else if (act === "cancel") {
      await api(`requests/${b.dataset.rid}`, { method: "DELETE" });
      toast("Request removed.");
      afterChange(null, null);
    } else if (act === "search") {
      await api(`requests/${b.dataset.rid}/search`, { method: "POST" });
      toast("Whisparr is searching for it again.");
      afterChange(null, null);
    } else if (act === "giveup") {
      await api(`requests/${b.dataset.rid}/give-up`, { method: "POST" });
      toast("Given up. Whisparr doesn't wait for it any more.");
      afterChange(null, null);
    } else if (act === "search-all") {
      const r = await api("requests/search-stalled", { method: "POST" });
      toast(`Whisparr is searching again for ${r.searched} request${r.searched === 1 ? "" : "s"}.`);
      afterChange(null, null);
    } else if (act === "feeling") {
      const f = b.dataset.feeling;
      await api("tag-feelings", { method: "POST", body: { id: b.dataset.id, feeling: f } });
      toast({
        love: "Loved: weighs most in the reels under a scene, and it's one of your favourite tags (a filter on Discover and Scenes).",
        like: "Liked: weighs in the reels under a scene, and it's one of your favourite tags (a filter on Discover and Scenes).",
        neutral: "Neutral again.",
        dislike: "Disliked: scenes with it stay out of the reel with your favourite performers, and in Discover only if tags you love or like outweigh it.",
        hate: "Hated: scenes with it are never recommended under a scene.",
      }[f]);
      await refreshStatus();
      render();
    } else if (act === "favorite") {
      const fav = b.dataset.favorite === "1";
      await api("favorite", { method: "POST", body: { kind: b.dataset.kind, id: b.dataset.id, favorite: fav } });
      toast(fav ? "Added to your StashDB favourites. Nothing is downloaded for it; use them as a filter." : "Removed from your StashDB favourites.");
      render();
    } else if (act === "follow") {
      const follow = b.dataset.follow === "1";
      await api("follow", { method: "POST", body: { kind: b.dataset.kind, id: b.dataset.id, follow } });
      toast(follow ? "Following. Its new scenes, from today on, are requested for you every hour; it's under Saved searches." : "Not following any more. Its requests so far stay.");
      render();
    }
  } catch (err) {
    if (err.status !== 401) toast(err.message, true);
    if (act !== "follow") afterChange(null, null, true);
  } finally {
    b.classList.remove("busy");
  }
});

function afterChange(sceneId, state, failed = false) {
  refreshStatus().catch(() => {});
  if (sceneId && state) rememberState(sceneId, state);
  const p = location.pathname;
  if (p.startsWith("/scene/") || p === "/requests" || (failed && sceneId === null && p === "/")) return render();
  if (sceneId && state) {
    $$(`.scene-card[data-scene="${CSS.escape(sceneId)}"]`).forEach((card) => {
      $(".quick", card).innerHTML = sceneQuick({ id: sceneId, state }).s;
      $(".progress", card)?.remove();
    });
  } else if (!failed) {
    render();
  }
}

// ---------------------------------------------------------------------------------------
// Pickers (tags, performers, studios) with type-ahead
// ---------------------------------------------------------------------------------------

const PICK_SOURCES = {
  tags: async (q) => (await api("tags" + qs({ q }))).tags.map((t) => ({ id: t.id, name: t.name, hint: t.category?.name })),
  performers: async (q) => (await api("performers" + qs({ q, per_page: 10, sort: "POPULARITY" }))).performers.map((p) => ({ id: p.id, name: p.name, hint: p.disambiguation || `${p.scene_count} scenes` })),
  studios: async (q) => (await api("studios" + qs({ q, per_page: 10 }))).studios.map((s) => ({ id: s.id, name: s.name, hint: s.parent?.name })),
};

function picker(el, { kind, placeholder, values, chip = "inc", onChange }) {
  let items = [...values];
  let hits = [];
  let hi = -1;
  el.classList.add("picker");
  el.innerHTML = html`<div class="box"><span class="chips"></span><input type="text" placeholder="${placeholder}" autocomplete="off" aria-label="${placeholder}"></div><div class="suggest" hidden></div>`.s;
  const box = $(".chips", el), input = $("input", el), sug = $(".suggest", el);
  const draw = () => {
    box.innerHTML = items.map((v) => html`<span class="chip ${chip}">${v.name}<button class="x" type="button" data-rm="${v.id}" aria-label="Remove ${v.name}">${icon("x")}</button></span>`.s).join("");
  };
  const drawSug = () => {
    sug.hidden = !hits.length;
    sug.innerHTML = hits.map((h, i) => html`<button type="button" data-i="${i}" class="${i === hi ? "hi" : ""}">${h.name}${h.hint ? html`<small>${h.hint}</small>` : ""}</button>`.s).join("");
  };
  const add = (h) => {
    if (!items.some((v) => v.id === h.id)) { items.push({ id: h.id, name: h.name }); draw(); onChange(items); }
    input.value = ""; hits = []; hi = -1; drawSug();
  };
  const lookup = debounce(async () => {
    const q = input.value.trim();
    if (q.length < 2) { hits = []; drawSug(); return; }
    try { hits = (await PICK_SOURCES[kind](q)).filter((h) => !items.some((v) => v.id === h.id)); } catch { hits = []; }
    hi = hits.length ? 0 : -1;
    drawSug();
  }, 220);
  input.addEventListener("input", lookup);
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" && hits.length) { e.preventDefault(); hi = (hi + 1) % hits.length; drawSug(); }
    else if (e.key === "ArrowUp" && hits.length) { e.preventDefault(); hi = (hi - 1 + hits.length) % hits.length; drawSug(); }
    else if (e.key === "Enter") { e.preventDefault(); if (hits[hi]) add(hits[hi]); }
    else if (e.key === "Escape") { hits = []; drawSug(); }
    else if (e.key === "Backspace" && !input.value && items.length) { items.pop(); draw(); onChange(items); }
  });
  input.addEventListener("blur", () => setTimeout(() => { hits = []; drawSug(); }, 150));
  sug.addEventListener("mousedown", (e) => { const b = e.target.closest("[data-i]"); if (b) { e.preventDefault(); add(hits[+b.dataset.i]); } });
  box.addEventListener("click", (e) => { const b = e.target.closest("[data-rm]"); if (b) { items = items.filter((v) => v.id !== b.dataset.rm); draw(); onChange(items); } });
  draw();
  return { set(v) { items = [...v]; draw(); } };
}

// ---------------------------------------------------------------------------------------
// The scene browser: filters (tags in and out, performers, studios, dates, favourites)
// over StashDB's scene query, with infinite scroll. Filters live in the URL.
// ---------------------------------------------------------------------------------------

// The collapsed filter panel's button, with how many filters are on.
const filterSummary = (n) => html`<summary class="btn small ghost">${icon("settings")}Filters<span class="fcount" ${n ? "" : "hidden"}>${n}</span></summary>`;
function setFilterCount(el, n) {
  const c = $(".fcount", el);
  if (c) { c.textContent = n; c.hidden = !n; }
}

const SORTS = [
  ["TRENDING:desc", "Trending"],
  ["DATE:desc", "Newest"],
  ["DATE:asc", "Oldest"],
  ["POPULARITY:desc", "Most popular"],
  ["CREATED_AT:desc", "Newly added to StashDB"],
  ["DURATION:desc", "Longest"],
  ["TITLE:asc", "Title A to Z"],
];

// onFilters(words, f): told what the filters match, in words (and the filters themselves), at
// the start and on every change.
async function sceneBrowser(el, ctx, { fixed = {}, fixedNames = {}, defaults = {}, hide = [], onFilters = null } = {}) {
  const params = new URLSearchParams(location.search);
  const f = {
    q: params.get("q") || "",
    sort: params.get("sort") || defaults.sort || "DATE",
    dir: params.get("dir") || defaults.dir || (params.get("sort") === "TITLE" ? "asc" : "desc"),
    tags: (params.get("tags") ?? defaults.tags ?? "").split(",").filter(Boolean),
    tag_mode: params.get("tag_mode") || "all",
    not_tags: (params.get("not_tags") || "").split(",").filter(Boolean),
    fav_tags: params.get("fav_tags") || "",
    performers: (params.get("performers") || "").split(",").filter(Boolean),
    studios: (params.get("studios") || "").split(",").filter(Boolean),
    sub_studios: params.get("sub_studios") ?? defaults.sub_studios ?? "",
    after: params.get("after") || "",
    before: params.get("before") || "",
    favorites: params.get("favorites") || "",
    hide_have: params.get("hide_have") || "",
  };
  // Names for the chips of IDs that came in the URL.
  const names = await api("names" + qs({ tags: [...f.tags, ...f.not_tags], performers: f.performers, studios: f.studios }));
  if (!ctx.alive()) return;
  const named = (ids, kind) => ids.map((id) => ({ id, name: names[kind]?.[id] || id.slice(0, 8) }));
  // The chips' names, for a saved search's name and summary.
  const labels = { tags: named(f.tags, "tags"), not_tags: named(f.not_tags, "tags"), performers: named(f.performers, "performers"), studios: named(f.studios, "studios") };

  const activeCount = () => [f.q, f.tags.length, f.fav_tags, f.not_tags.length, f.performers.length, f.studios.length, f.after, f.before, f.favorites, f.hide_have].filter(Boolean).length;
  el.innerHTML = html`
    <details class="filters">
      ${filterSummary(activeCount())}
      <div class="filter-row">
        <label class="field grow"><span>Title</span><input class="input" id="f-q" type="search" placeholder="Words in the title" value="${f.q}"></label>
        <label class="field"><span>Sort</span><select class="input" id="f-sort">${SORTS.map(([v, l]) => html`<option value="${v}" ${v === `${f.sort}:${f.dir}` ? "selected" : ""}>${l}</option>`)}</select></label>
        <label class="field"><span>Favourites</span><select class="input" id="f-fav">
          <option value="">All scenes</option>
          <option value="ALL" ${f.favorites === "ALL" ? "selected" : ""}>Favourite performers or studios</option>
          <option value="PERFORMER" ${f.favorites === "PERFORMER" ? "selected" : ""}>Favourite performers</option>
          <option value="STUDIO" ${f.favorites === "STUDIO" ? "selected" : ""}>Favourite studios</option>
        </select></label>
      </div>
      <div class="filter-row">
        <div class="field grow"><span>With tags</span><div id="f-tags"></div></div>
        <div class="field"><span>Match</span><div class="seg" id="f-mode"><button type="button" data-v="all" class="${f.tag_mode === "all" ? "on" : ""}">All tags</button><button type="button" data-v="any" class="${f.tag_mode === "any" ? "on" : ""}">Any tag</button></div></div>
        <div class="field grow"><span>Without tags</span><div id="f-not"></div></div>
      </div>
      ${status.fav_tags?.length || f.fav_tags ? html`<div class="filter-row"><label class="check"><input type="checkbox" id="f-favtags" ${f.fav_tags ? "checked" : ""}> With any of the tags I love or like${status.fav_tags?.length ? ` (${status.fav_tags.map((t) => t.name).join(", ")})` : " (none yet: pick Love or Like on a tag's page)"}</label></div>` : ""}
      <div class="filter-row">
        ${hide.includes("performers") ? "" : html`<div class="field grow"><span>${fixed.performers ? "Also with" : "Performers"}</span><div id="f-perf"></div></div>`}
        ${hide.includes("studios") ? "" : html`<div class="field grow"><span>Studios</span><div id="f-studio"></div></div>`}
        <label class="field"><span>Released after</span><input class="input" type="date" id="f-after" value="${f.after}"></label>
        <label class="field"><span>Released before</span><input class="input" type="date" id="f-before" value="${f.before}"></label>
      </div>
      <div class="filter-row">
        <label class="check"><input type="checkbox" id="f-hide" ${f.hide_have ? "checked" : ""}> Hide scenes I already have or requested</label>
        ${fixed.studios ? html`<label class="check"><input type="checkbox" id="f-sub" ${f.sub_studios ? "checked" : ""}> Include its sub-studios</label>` : ""}
        <button class="btn small" type="button" id="f-save" style="margin-left:auto" title="Request new scenes that match these filters, every hour">${icon("bookmark")}Save search</button>
        <button class="btn small ghost" type="button" id="f-clear">Clear filters</button>
      </div>
      <form class="filter-row save-search" id="f-saveform" hidden>
        <label class="field grow"><span>Saved search's name</span><input class="input" name="wname" maxlength="80" required></label>
        <label class="field"><span>Scenes released from</span><input class="input" type="date" name="since" required></label>
        ${me.is_admin || me.auto_approve ? html`<label class="check"><input type="checkbox" name="ask_first"> Ask me before requesting</label>` : html`<span class="note">Its requests wait for an admin, like yours do.</span>`}
        <button class="btn small primary">Save</button>
        <p class="note">Every hour, new scenes that match these filters are requested, and older ones (released from that date on) that get tagged to match later. Sorting and the release dates above don't count.</p>
      </form>
    </details>
    <div class="section-head"><h2 id="b-count" class="sub" style="font-size:15px;color:var(--muted);font-weight:500"></h2></div>
    <div class="grid scenes" id="b-grid"></div>
    <div class="load-more" id="b-more"></div>`.s;

  const sync = () => {
    const url = location.pathname + qs({
      q: f.q, sort: f.sort !== (defaults.sort || "DATE") || f.dir !== (defaults.dir || "desc") ? f.sort : "",
      dir: f.dir !== (defaults.dir || "desc") || f.sort !== (defaults.sort || "DATE") ? f.dir : "",
      tags: f.tags.join(",") !== (defaults.tags || "") ? (f.tags.length ? f.tags : "-") : "",
      tag_mode: f.tag_mode !== "all" ? f.tag_mode : "", not_tags: f.not_tags, fav_tags: f.fav_tags,
      performers: f.performers, studios: f.studios,
      sub_studios: fixed.studios ? (f.sub_studios ? "1" : "0") : "",
      after: f.after, before: f.before, favorites: f.favorites, hide_have: f.hide_have,
    }).replace("tags=-", "tags=");
    history.replaceState(null, "", url);
    setFilterCount(el, activeCount());
    onFilters?.(describe(), f);
    load(true);
  };

  picker($("#f-tags", el), { kind: "tags", placeholder: "Add a tag", values: labels.tags, onChange: (v) => { labels.tags = [...v]; f.tags = v.map((x) => x.id); sync(); } });
  picker($("#f-not", el), { kind: "tags", placeholder: "Leave out a tag", chip: "exc", values: labels.not_tags, onChange: (v) => { labels.not_tags = [...v]; f.not_tags = v.map((x) => x.id); sync(); } });
  if ($("#f-perf", el)) picker($("#f-perf", el), { kind: "performers", placeholder: "Add a performer", values: labels.performers, onChange: (v) => { labels.performers = [...v]; f.performers = v.map((x) => x.id); sync(); } });
  if ($("#f-studio", el)) picker($("#f-studio", el), { kind: "studios", placeholder: "Add a studio", values: labels.studios, onChange: (v) => { labels.studios = [...v]; f.studios = v.map((x) => x.id); sync(); } });
  $("#f-q", el).addEventListener("input", debounce((e) => { f.q = e.target.value.trim(); sync(); }, 400));
  $("#f-sort", el).onchange = (e) => { [f.sort, f.dir] = e.target.value.split(":"); sync(); };
  $("#f-fav", el).onchange = (e) => { f.favorites = e.target.value; sync(); };
  $("#f-after", el).onchange = (e) => { f.after = e.target.value; if (f.after) { f.before = ""; $("#f-before", el).value = ""; } sync(); };
  $("#f-before", el).onchange = (e) => { f.before = e.target.value; if (f.before) { f.after = ""; $("#f-after", el).value = ""; } sync(); };
  $("#f-hide", el).onchange = (e) => { f.hide_have = e.target.checked ? "1" : ""; sync(); };
  if ($("#f-favtags", el)) $("#f-favtags", el).onchange = (e) => { f.fav_tags = e.target.checked ? "1" : ""; sync(); };
  if ($("#f-sub", el)) $("#f-sub", el).onchange = (e) => { f.sub_studios = e.target.checked ? "1" : ""; sync(); };
  $("#f-mode", el).onclick = (e) => {
    const b = e.target.closest("[data-v]");
    if (!b) return;
    f.tag_mode = b.dataset.v;
    $$("#f-mode button", el).forEach((x) => x.classList.toggle("on", x === b));
    if (f.tags.length > 1) sync();
  };
  $("#f-clear", el).onclick = () => { history.replaceState(null, "", location.pathname); render(); };

  // Saving the filters as a saved search: what they match, in words, and as a query.
  const performers = () => [...(fixed.performers || []), ...f.performers.filter((p) => !(fixed.performers || []).includes(p))];
  const describe = () => {
    const nm = (list) => list.map((x) => x.name);
    const perf = [...(fixedNames.performers || []), ...labels.performers];
    const studios = fixedNames.studios || labels.studios;
    const parts = [];
    if (labels.tags.length) parts.push(nm(labels.tags).join(f.tag_mode === "any" ? " or " : " + "));
    if (f.fav_tags) parts.push("tags I love or like");
    if (perf.length) parts.push(nm(perf).join(" & "));
    if (studios.length) parts.push(nm(studios).join(" or ") + (fixed.studios && f.sub_studios ? " (with sub-studios)" : ""));
    if (f.favorites) parts.push({ ALL: "StashDB favourites", PERFORMER: "Favourite performers", STUDIO: "Favourite studios" }[f.favorites]);
    if (f.q) parts.push(`“${f.q}”`);
    if (labels.not_tags.length) parts.push("not " + nm(labels.not_tags).join(", "));
    return parts.join(" · ");
  };
  onFilters?.(describe(), f);
  const watchQuery = () => qs(Object.fromEntries(Object.entries({
    q: f.q, tags: f.tags, tag_mode: f.tags.length > 1 && f.tag_mode !== "all" ? f.tag_mode : "", not_tags: f.not_tags, fav_tags: f.fav_tags,
    performers: performers(), performer_mode: performers().length > 1 ? "all" : "",
    studios: fixed.studios || f.studios, sub_studios: fixed.studios && f.sub_studios ? "1" : "", favorites: f.favorites,
  }).filter(([, v]) => !(Array.isArray(v) && !v.length)))).slice(1);
  const saveForm = $("#f-saveform", el);
  $("#f-save", el).onclick = () => {
    if (!saveForm.hidden) { saveForm.hidden = true; return; }
    if (!(f.q || f.tags.length || f.fav_tags || performers().length || (fixed.studios || f.studios).length || f.favorites)) {
      return toast("Pick a tag, performer, studio or favourites first. Without one, a saved search would request every new scene.", true);
    }
    saveForm.wname.value = describe().slice(0, 80);
    saveForm.since.value = localToday();
    saveForm.hidden = false;
    saveForm.wname.select();
  };
  saveForm.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("button.primary", saveForm);
    btn.classList.add("busy");
    try {
      await api("watches", { method: "POST", body: { name: saveForm.wname.value, since: saveForm.since.value, ask_first: !!saveForm.ask_first?.checked, query: watchQuery(), summary: describe() } });
      saveForm.hidden = true;
      toast("Saved. New matching scenes are requested every hour; see Saved searches.");
    } catch (err) { toast(err.message, true); } finally { btn.classList.remove("busy"); }
  };

  const grid = $("#b-grid", el), more = $("#b-more", el), countEl = $("#b-count", el);
  let page = 0, total = 0, loading = false, seq = 0, shown = 0, mem = null;
  const HAVE = new Set(["available", "processing", "downloading", "pending"]);

  function draw(scenes) {
    if (f.hide_have) scenes = scenes.filter((s) => !HAVE.has(s.state?.status));
    grid.insertAdjacentHTML("beforeend", scenes.map((s) => sceneCard(s).s).join(""));
    countEl.textContent = total ? `${total.toLocaleString()} scene${total === 1 ? "" : "s"}` : "";
    if (!total) grid.innerHTML = html`<div class="empty" style="grid-column:1/-1">No scenes match these filters.</div>`.s;
    more.innerHTML = shown < total ? html`<button class="btn" id="b-next">Load more</button>`.s : "";
  }
  async function load(reset) {
    const my = ++seq;
    if (reset) { page = 0; shown = 0; grid.innerHTML = ""; total = 0; mem = rememberList(); }
    loading = true;
    more.innerHTML = html`<div class="spinner"></div>`.s;
    try {
      const res = await api("scenes" + qs({
        q: f.q, sort: f.sort, dir: f.dir,
        tags: f.tags, tag_mode: f.tag_mode, not_tags: f.not_tags, fav_tags: f.fav_tags,
        performers: [...(fixed.performers || []), ...f.performers.filter((p) => !(fixed.performers || []).includes(p))],
        performer_mode: "all",
        studios: fixed.studios || f.studios, sub_studios: fixed.studios && f.sub_studios ? "1" : "",
        after: f.after, before: f.before, favorites: f.favorites, page: page + 1, per_page: 40,
      }));
      if (my !== seq || !ctx.alive()) return;
      page = res.page;
      total = res.count;
      shown += res.scenes.length;
      draw(res.scenes);
      mem.items.push(...res.scenes);
      Object.assign(mem, { page, total, shown, at: Date.now() });
    } catch (e) {
      if (my !== seq || !ctx.alive()) return;
      more.innerHTML = errorBox(e).s;
    } finally {
      loading = false;
    }
  }
  more.addEventListener("click", (e) => { if (e.target.closest("#b-next")) load(false); });
  const io = new IntersectionObserver((entries) => {
    if (!ctx.alive()) { io.disconnect(); return; }
    if (entries[0].isIntersecting && !loading && shown && shown < total) load(false);
  }, { rootMargin: "600px" });
  io.observe(more);
  const old = recalledList(ctx);
  if (!old) return load(true);
  mem = old;
  ({ page, total, shown } = old);
  draw(old.items);
  restoreScroll(old.scroll);
}

// ---------------------------------------------------------------------------------------
// Photo viewer: full-size pictures, paged with the arrows, the keyboard or a swipe
// ---------------------------------------------------------------------------------------

function lightbox(images, start = 0, title = "") {
  if (!images?.length) return;
  closeLightbox();
  let i = Math.max(0, start);
  const many = images.length > 1;
  // Actual size, and moving around in it, is for a mouse; on touch screens pictures just fit.
  const mouse = matchMedia("(hover: hover) and (pointer: fine)").matches;
  const back = document.activeElement;
  const el = document.createElement("div");
  el.className = "lightbox loading";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-modal", "true");
  el.setAttribute("aria-label", title ? `Photos of ${title}` : "Photos");
  el.innerHTML = html`
    <button type="button" class="iconbtn lb-close" aria-label="Close">${icon("x")}</button>
    ${mouse ? html`<button type="button" class="iconbtn lb-zoom" aria-label="Actual size" title="Actual size" hidden>${icon("zoomIn")}</button>` : ""}
    ${many ? html`<button type="button" class="iconbtn lb-nav lb-prev" aria-label="Previous photo">${icon("chevronLeft")}</button>` : ""}
    <figure class="lb-stage"><img alt="" draggable="false"><div class="spinner"></div></figure>
    ${many ? html`<button type="button" class="iconbtn lb-nav lb-next" aria-label="Next photo">${icon("chevron")}</button>` : ""}
    <div class="lb-count" aria-live="polite"></div>`.s;
  const img = $("img", el), stage = $(".lb-stage", el), count = $(".lb-count", el), zoomBtn = $(".lb-zoom", el);
  let zoomed = false, x = 0, y = 0;

  // Zoomed: the picture at its own size, moved so it covers the stage where it's big enough.
  const place = () => {
    const w = stage.clientWidth, h = stage.clientHeight, W = img.naturalWidth, H = img.naturalHeight;
    x = W <= w ? (w - W) / 2 : Math.min(0, Math.max(w - W, x));
    y = H <= h ? (h - H) / 2 : Math.min(0, Math.max(h - H, y));
    img.style.transform = `translate(${x}px, ${y}px)`;
  };
  // Fitted: offer actual size only when the picture had to be shrunk to fit.
  const updateZoomable = () => {
    if (zoomed) return;
    const ok = mouse && img.naturalWidth > 0 &&
      (img.naturalWidth > img.clientWidth + 1 || img.naturalHeight > img.clientHeight + 1);
    el.classList.toggle("can-zoom", ok);
    if (zoomBtn) zoomBtn.hidden = !ok;
  };
  const zoom = (on, cx, cy) => {
    if (on === zoomed) return;
    if (on) {
      // Keep the point under the cursor (or the middle) where it is.
      const r = img.getBoundingClientRect();
      cx ??= r.left + r.width / 2;
      cy ??= r.top + r.height / 2;
      const fx = (cx - r.left) / r.width, fy = (cy - r.top) / r.height;
      zoomed = true;
      el.classList.add("zoomed");
      const s = stage.getBoundingClientRect();
      x = cx - s.left - fx * img.naturalWidth;
      y = cy - s.top - fy * img.naturalHeight;
      place();
    } else {
      zoomed = false;
      el.classList.remove("zoomed");
      img.style.transform = "";
      updateZoomable();
    }
    if (zoomBtn) {
      zoomBtn.innerHTML = icon(zoomed ? "zoomOut" : "zoomIn").s;
      zoomBtn.title = zoomed ? "Fit to screen" : "Actual size";
      zoomBtn.setAttribute("aria-label", zoomBtn.title);
    }
  };
  const show = (n) => {
    zoom(false);
    i = (n + images.length) % images.length;
    el.classList.add("loading");
    el.classList.remove("can-zoom");
    if (zoomBtn) zoomBtn.hidden = true;
    img.onload = () => { el.classList.remove("loading"); updateZoomable(); };
    img.onerror = () => el.classList.remove("loading");
    img.src = imgUrl(images[i].url);
    count.textContent = many ? `${i + 1} / ${images.length}` : "";
    if (many) for (const d of [1, -1]) new Image().src = imgUrl(images[(i + d + images.length) % images.length].url);
  };
  const onKey = (e) => {
    if (e.key === "Escape") { if (zoomed) zoom(false); else closeLightbox(); }
    else if (many && e.key === "ArrowRight") show(i + 1);
    else if (many && e.key === "ArrowLeft") show(i - 1);
  };
  const onResize = () => (zoomed ? place() : updateZoomable());
  el._close = () => {
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("resize", onResize);
    document.body.classList.remove("lb-open");
    el.remove();
    back?.focus?.();
  };

  // On the picture: drag to move around when zoomed; otherwise a swipe pages. A tap outside
  // the picture closes, a click on it zooms or (zoomed, without dragging) fits it again.
  // A drag also ends in a click, which is then ignored.
  let drag = null, swallow = false;
  stage.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    drag = { x0: e.clientX, y0: e.clientY, x, y, moved: false };
    if (zoomed) {
      e.preventDefault();
      stage.setPointerCapture(e.pointerId);
      stage.classList.add("dragging");
    }
  });
  stage.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x0, dy = e.clientY - drag.y0;
    if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
    if (zoomed) { x = drag.x + dx; y = drag.y + dy; place(); }
  });
  const endDrag = (e) => {
    if (!drag) return;
    const d = drag;
    drag = null;
    stage.classList.remove("dragging");
    if (zoomed) { swallow = true; if (!d.moved) zoom(false); return; }
    const dx = e.clientX - d.x0;
    if (many && Math.abs(dx) > 50) { swallow = true; show(i + (dx < 0 ? 1 : -1)); }
  };
  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", () => { drag = null; stage.classList.remove("dragging"); });
  el.addEventListener("wheel", (e) => {
    if (!zoomed) return;
    e.preventDefault();
    x -= e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX;
    y -= e.shiftKey && !e.deltaX ? 0 : e.deltaY;
    place();
  }, { passive: false });
  el.addEventListener("click", (e) => {
    if (swallow) { swallow = false; return; }
    if (e.target.closest(".lb-close")) closeLightbox();
    else if (e.target.closest(".lb-zoom")) zoom(!zoomed);
    else if (e.target.closest(".lb-prev")) show(i - 1);
    else if (e.target.closest(".lb-next")) show(i + 1);
    else if (e.target === img) { if (el.classList.contains("can-zoom")) zoom(true, e.clientX, e.clientY); }
    else if (e.target === el || e.target === stage) closeLightbox();
  });
  document.addEventListener("keydown", onKey);
  window.addEventListener("resize", onResize);
  document.body.classList.add("lb-open");
  document.body.append(el);
  $(".lb-close", el).focus();
  show(i);
}

function closeLightbox() {
  $(".lightbox")?._close();
}

// ---------------------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------------------

// StashDB's rows are kept an hour on the server; Reload makes them again.
async function viewDiscover(view, ctx, fresh = false) {
  const d = await api("discover" + (fresh ? "?fresh=1" : ""));
  if (!ctx.alive()) return;
  const parts = [html`<div class="page-title"><h1>Discover</h1><span class="sub" title="${new Date(d.updated * 1000).toLocaleString()}">Updated ${fmtAgo(d.updated)}</span>
    <button class="btn small ghost" type="button" id="reload" style="margin-left:auto" title="The rows from StashDB are kept for an hour; this asks StashDB again now">${icon("refresh")}Reload</button></div>`];
  if (d.whisparr_error) parts.push(html`<div class="error-box">Whisparr: ${d.whisparr_error}</div>`);
  if (d.requests.length) parts.push(slider("Your recent requests", d.requests, "reqs", "/requests", (r) => requestRow(r, { compact: true })));
  const rows = Object.fromEntries(d.rows.map((r) => [r.key, r]));
  const errors = new Set(d.rows.map((r) => r.error));
  if (errors.size === 1 && d.rows[0].error) {
    // StashDB is out (no key, refused, unreachable): say so once instead of per row.
    parts.splice(1, 0, errorBox(new Error(d.rows[0].error)));
    d.rows = [];
    for (const k in rows) delete rows[k];
  }
  const sceneRow = (key) => {
    const r = rows[key];
    if (!r) return;
    if (r.error) parts.push(html`<section class="section"><div class="section-head"><h2>${r.title}</h2></div>${errorBox(new Error(r.error))}</section>`);
    else if (r.scenes.length || !["favorites", "favtags"].includes(key)) parts.push(slider(r.title, r.scenes, "scenes", r.link, sceneCard));
  };
  sceneRow("trending");
  sceneRow("favorites");
  sceneRow("favtags");
  if (d.followed?.performers?.length) parts.push(slider("Performers you follow", d.followed.performers.map((p) => ({ ...p, followed: true })), "people", null, personCard));
  sceneRow("added");
  sceneRow("new");
  if (d.followed?.studios?.length) parts.push(slider("Studios you follow", d.followed.studios.map((s) => ({ ...s, followed: true })), "people", null, studioCard));
  sceneRow("popular");
  view.innerHTML = html`<div class="page">${parts}</div>`.s;
  $("#reload", view).onclick = async (e) => {
    const btn = e.currentTarget, y = window.scrollY;
    btn.classList.add("busy");
    try {
      await viewDiscover(view, ctx, true);
      restoreScroll(y);
    } catch (err) { btn.classList.remove("busy"); toast(err.message, true); }
  };
}

async function viewScenes(view, ctx) {
  view.innerHTML = html`<div class="page"><div class="page-title"><h1>Scenes</h1></div><div class="section" id="browser"></div></div>`.s;
  await sceneBrowser($("#browser", view), ctx, { defaults: { sort: "TRENDING", dir: "desc" } });
}

function sceneActions(s) {
  const st = s.state, r = s.request, out = [];
  if (canRequest(st)) out.push(html`<button class="btn primary" data-act="request" data-id="${s.id}">${icon("plus")}${st.status === "none" ? "Request" : "Request again"}</button>`);
  if (st.status === "pending" && r) {
    if (me.is_admin) {
      out.push(html`<button class="btn ok" data-act="approve" data-rid="${r.id}">${icon("check")}Approve</button>`);
      out.push(html`<button class="btn danger" data-act="decline" data-rid="${r.id}">${icon("x")}Decline</button>`);
    }
    if (me.is_admin || r.user_id === me.id) out.push(html`<button class="btn ghost" data-act="cancel" data-rid="${r.id}">Cancel request</button>`);
  }
  if (st.status === "available") out.push(watchButton(st));
  if (mayNudge(r)) {
    out.push(html`<button class="btn" data-act="search" data-rid="${r.id}" title="Whisparr searches the indexers again now">${icon("refresh")}Search again</button>`);
    out.push(html`<button class="btn ghost" data-act="giveup" data-rid="${r.id}" title="Whisparr stops waiting for a release; it can be requested again later">${icon("x")}Give up</button>`);
  }
  if (me.is_admin && st.whisparr_id && status.whisparr_url) out.push(html`<a class="btn ghost" href="${status.whisparr_url}/movie/${st.whisparr_id}" target="_blank" rel="noreferrer">${icon("external")}Whisparr</a>`);
  out.push(html`<a class="btn ghost" href="${s.stashdb_url}" target="_blank" rel="noreferrer">${icon("external")}StashDB</a>`);
  return out;
}

async function viewScene(view, id, ctx) {
  const s = await api("scene/" + id);
  if (!ctx.alive()) return;
  document.title = s.title || document.title;
  const im = pickImage(s.images, true);
  const groups = {};
  for (const t of s.tags || []) (groups[t.category?.name || "Other"] ||= []).push(t);
  const similar = (s.tags || []).filter((t) => ["ACTION", "SCENE"].includes(t.category?.group)).slice(0, 3).map((t) => t.id);
  const facts = [
    ["Studio", s.studio ? html`<a href="/studio/${s.studio.id}">${s.studio.name}</a>` : ""],
    ["Network", s.studio?.parent ? html`<a href="/studio/${s.studio.parent.id}">${s.studio.parent.name}</a>` : ""],
    ["Released", fmtDate(s.release_date)],
    ["Produced", fmtDate(s.production_date)],
    ["Length", fmtDur(s.duration)],
    ["Code", s.code],
    ["Director", s.director],
    ["Requested", s.request ? `${fmtAgo(s.request.created)}${s.request.requested_by ? ` by ${s.request.requested_by}` : ""}` : ""],
  ].filter(([, v]) => v);
  view.innerHTML = html`<div class="page flush">
    <section class="hero">
      <div class="backdrop">${im ? html`<img src="${imgUrl(im, 1280)}" alt="">` : ""}</div>
      <div class="hero-body">
        ${im ? html`<button type="button" class="hero-poster zoomable" data-photo="${s.images.findIndex((x) => x.url === im)}" aria-label="Show the pictures">
          <img src="${imgUrl(im, 1280)}" alt="">${s.images.length > 1 ? html`<span class="photo-count">${icon("photos")}${s.images.length}</span>` : ""}</button>`
        : html`<div class="hero-poster">${noImg()}</div>`}
        <div class="hero-info">
          <div class="kicker">${s.studio ? html`<a href="/studio/${s.studio.id}">${s.studio.name}</a>` : ""}${s.studio?.parent ? html`<span style="color:var(--muted)">· ${s.studio.parent.name}</span>` : ""}</div>
          <h1>${s.title || "Untitled"}</h1>
          <div class="facts">${[fmtDate(s.release_date), fmtDur(s.duration), s.code].filter(Boolean).map((x) => html`<span>${x}</span>`)}</div>
          <div>${statePill(s.state)}</div>
          ${s.state.status === "downloading" ? html`<div class="dl-bar"><i style="width:${s.state.progress || 0}%"></i></div><div class="note">${s.state.timeleft ? `About ${s.state.timeleft} left` : ""}</div>` : ""}
          ${s.state.status === "processing" ? html`<div class="note ${s.request?.stalled_days ? "warn-text" : ""}">${s.request?.stalled_days ? stalledNote(s.request) : ""} Whisparr wants this scene and grabs it when a release shows up.</div>` : ""}
          ${s.state.status === "failed" && s.request?.error ? html`<div class="note bad-text">${s.request.error}</div>` : ""}
          <div class="btns">${sceneActions(s)}</div>
        </div>
      </div>
    </section>
    <div class="detail-grid">
      <div class="section" style="gap:28px">
        ${s.performers?.length ? html`<section class="section"><div class="section-head"><h2>Performers</h2></div>
          <div class="cast">${s.performers.map((p) => {
            const pim = pickImage(p.performer.images, false);
            return html`<a href="/performer/${p.performer.id}"><div class="face">${pim ? html`<img src="${imgUrl(pim, 240)}" alt="" loading="lazy">` : ""}</div><span>${performerName(p)}${p.as && p.as !== p.performer.name ? html`<small>${p.performer.name}</small>` : p.performer.disambiguation ? html`<small>${p.performer.disambiguation}</small>` : ""}${castAges(p.performer, s)}</span></a>`;
          })}</div></section>` : ""}
        ${s.details ? html`<section class="section"><div class="section-head"><h2>About</h2></div><p class="prose">${s.details}</p></section>` : ""}
        ${s.tags?.length ? html`<section class="section"><div class="section-head"><h2>Tags</h2>${similar.length ? html`<a class="more" href="/scenes${qs({ tags: similar, sort: "POPULARITY" })}">More like this${icon("chevron")}</a>` : ""}</div>
          <div class="tag-groups">${Object.entries(groups).sort(([a], [b]) => (a === "Other") - (b === "Other") || a.localeCompare(b)).map(([cat, ts]) => html`<div class="tag-group"><h3>${cat}</h3><div class="chips">${ts.map((t) => tagChip(t))}</div></div>`)}</div></section>` : ""}
      </div>
      <aside class="facts-card"><dl>${facts.map(([k, v]) => html`<div><dt>${k}</dt><dd>${v}</dd></div>`)}</dl></aside>
    </div>
    <div id="similar"></div>
  </div>`.s;
  $(".page", view).addEventListener("click", (e) => { const b = e.target.closest("[data-photo]"); if (b) lightbox(s.images, +b.dataset.photo, s.title); });
  similarScenes($("#similar", view), id, ctx);
}

// The reels under a scene (with favourite performers, Discover, Together again; see
// App.similar). Loaded
// after the page, which is complete without them.
async function similarScenes(el, id, ctx) {
  let r;
  try { r = await api(`scene/${id}/similar`); } catch { return; }
  if (!ctx.alive() || !r.rows.length) return;
  el.outerHTML = r.rows.map((row) => html`<section class="section">
    <div class="section-head"><h2>${row.title}</h2>${row.sub ? html`<span class="sub">${row.sub}</span>` : ""}</div>
    <div class="slider scenes">${row.scenes.map(sceneCard)}</div>
  </section>`.s).join("");
}

function favoriteButton(kind, item) {
  if (!me.is_admin) return "";
  return item.is_favorite
    ? html`<button class="btn" data-act="favorite" data-kind="${kind}" data-id="${item.id}" data-favorite="0" title="Remove from your StashDB favourites">${icon("heartFill")}Favourite</button>`
    : html`<button class="btn" data-act="favorite" data-kind="${kind}" data-id="${item.id}" data-favorite="1" title="Add to your StashDB favourites: a filter on Discover, Scenes and the lists. Nothing is downloaded for it.">${icon("heart")}Add to favourites</button>`;
}

function followButton(kind, item) {
  const f = item.follow || {};
  if (f.error) return html`<span class="note bad-text">${f.error}</span>`;
  return f.followed
    ? html`<button class="btn" data-act="follow" data-kind="${kind}" data-id="${item.id}" data-follow="0" title="Its new scenes${f.since ? ` (released from ${f.since})` : ""} are requested for you every hour. Unfollowing keeps the requests made so far.">${icon("bellOff")}Unfollow</button>`
    : html`<button class="btn primary" data-act="follow" data-kind="${kind}" data-id="${item.id}" data-follow="1" title="Scenes released from today on are requested for you every hour, like a saved search (it's listed there). Older ones can still be requested one by one.">${icon("bell")}Follow new scenes</button>`;
}

async function viewPerformer(view, id, ctx) {
  const p = await api("performer/" + id);
  if (!ctx.alive()) return;
  document.title = p.name;
  const im = pickImage(p.images, false);
  const back = pickImage(p.images, true);
  const meas = [p.band_size && p.cup_size ? `${p.band_size}${p.cup_size}` : p.cup_size, p.waist_size, p.hip_size].filter(Boolean).join("-");
  const mods = (list) => (list || []).map((m) => [m.location, m.description].filter(Boolean).join(": ")).join(", ");
  const ageNow = p.death_date ? "" : ageAt(p.birth_date) || p.age || "";
  const ageEnd = p.death_date ? ageAt(p.birth_date, p.death_date) : "";
  const facts = [
    ["Aliases", (p.aliases || []).join(", ")],
    ["Born", fmtDate(p.birth_date) + (ageNow ? ` (${ageNow})` : "")],
    ["Died", p.death_date ? fmtDate(p.death_date) + (ageEnd ? ` (aged ${ageEnd})` : "") : ""],
    ["Country", p.country],
    ["Ethnicity", titleCase(p.ethnicity)],
    ["Hair", titleCase(p.hair_color)],
    ["Eyes", titleCase(p.eye_color)],
    ["Height", p.height ? `${p.height} cm` : ""],
    ["Measurements", meas],
    ["Breasts", titleCase(p.breast_type)],
    ["Career", p.career_start_year ? `${p.career_start_year} to ${p.career_end_year || "now"}` : ""],
    ["Tattoos", mods(p.tattoos)],
    ["Piercings", mods(p.piercings)],
  ].filter(([, v]) => v && String(v).trim());
  const photos = p.images || [];
  view.innerHTML = html`<div class="page flush">
    <section class="hero person">
      <div class="backdrop">${back && back !== im ? html`<img src="${imgUrl(back, 1280)}" alt="">` : ""}</div>
      <div class="hero-body">
        ${im ? html`<button type="button" class="hero-poster portrait zoomable" data-photo="${photos.findIndex((x) => x.url === im)}" aria-label="Show the photos">
          <img src="${imgUrl(im, 800)}" alt="">${photos.length > 1 ? html`<span class="photo-count">${icon("photos")}${photos.length}</span>` : ""}</button>`
        : html`<div class="hero-poster portrait">${noImg("users")}</div>`}
        <div class="hero-info">
          <div class="kicker">${[titleCase(p.gender), p.country].filter(Boolean).join(" · ")}${p.is_favorite ? html`<span title="StashDB favourite">${icon("heartFill")}</span>` : ""}</div>
          <h1>${p.name}${p.death_date ? html` <span class="dagger" title="Died ${fmtDate(p.death_date)}">†</span>` : ""}</h1>
          ${p.disambiguation ? html`<div class="facts"><span>${p.disambiguation}</span></div>` : ""}
          <div class="facts"><span>${p.scene_count} scenes on StashDB</span>${p.follow?.followed ? html`<span>Following${p.follow.since ? ` since ${fmtDate(p.follow.since)}` : ""}</span>` : ""}</div>
          <div class="btns">${followButton("performer", p)}${favoriteButton("performer", p)}<a class="btn ghost" href="${p.stashdb_url}" target="_blank" rel="noreferrer">${icon("external")}StashDB</a></div>
        </div>
      </div>
    </section>
    ${photos.length > 1 ? html`<section class="section">
      <div class="section-head"><h2>Photos</h2><span class="note">${photos.length}</span></div>
      <div class="slider gallery">${photos.map((ph, i) => html`<button type="button" class="card thumb-btn" data-photo="${i}" aria-label="Photo ${i + 1} of ${photos.length}"><div class="thumb"><img src="${imgUrl(ph.url, 360)}" alt="" loading="lazy" decoding="async"></div></button>`)}</div>
    </section>` : ""}
    <div class="detail-grid">
      <div class="section"><div class="section-head"><h2>Scenes</h2></div><div class="section" id="browser"></div></div>
      ${facts.length ? html`<aside class="facts-card"><dl>${facts.map(([k, v]) => html`<div><dt>${k}</dt><dd>${v}</dd></div>`)}</dl></aside>` : ""}
    </div>
  </div>`.s;
  $(".page", view).addEventListener("click", (e) => { const b = e.target.closest("[data-photo]"); if (b) lightbox(photos, +b.dataset.photo, p.name); });
  await sceneBrowser($("#browser", view), ctx, { fixed: { performers: [id] }, fixedNames: { performers: [{ id, name: p.name }] } });
}

async function viewStudio(view, id, ctx) {
  const s = await api("studio/" + id);
  if (!ctx.alive()) return;
  document.title = s.name;
  const im = pickImage(s.images, true);
  const site = (s.urls || []).find((u) => /home|studio/i.test(u.site?.name || "")) || (s.urls || [])[0];
  view.innerHTML = html`<div class="page flush">
    <section class="hero compact">
      <div class="hero-body">
        <div class="hero-poster logo">${im ? html`<img src="${imgUrl(im)}" alt="">` : noImg("building")}</div>
        <div class="hero-info">
          <div class="kicker">${s.parent ? html`<a href="/studio/${s.parent.id}">${s.parent.name}</a>` : "Studio"}${s.is_favorite ? html`<span title="StashDB favourite">${icon("heartFill")}</span>` : ""}</div>
          <h1>${s.name}</h1>
          ${s.follow?.followed ? html`<div class="facts"><span>Following${s.follow.since ? ` since ${fmtDate(s.follow.since)}` : ""}</span></div>` : ""}
          <div class="btns">${followButton("studio", s)}${favoriteButton("studio", s)}<a class="btn ghost" href="${s.stashdb_url}" target="_blank" rel="noreferrer">${icon("external")}StashDB</a>${site ? html`<a class="btn ghost" href="${site.url}" target="_blank" rel="noreferrer">${icon("external")}Website</a>` : ""}</div>
          ${s.child_studios?.length ? html`<div class="chips">${s.child_studios.map((c) => html`<a class="chip" href="/studio/${c.id}">${c.name}</a>`)}</div>` : ""}
        </div>
      </div>
    </section>
    <div class="section"><div class="section-head"><h2>Scenes</h2></div><div class="section" id="browser"></div></div>
  </div>`.s;
  await sceneBrowser($("#browser", view), ctx, { fixed: { studios: [id] }, fixedNames: { studios: [{ id, name: s.name }] }, defaults: { sub_studios: s.child_studios?.length ? "1" : "" }, hide: ["studios"] });
}

// A tag's page: the tag, and its scenes. Once the filters are more (or other) than the tag,
// the heading says what they match instead, with the way back to just the tag.
async function viewTag(view, id, ctx) {
  const t = await api("tag/" + id);
  if (!ctx.alive()) return;
  document.title = t.name;
  view.innerHTML = html`<div class="page">
    <div class="section" style="gap:8px" id="tag-head">
      <div class="page-title"><h1>${t.name}</h1>${t.category ? html`<a class="chip" href="/tags#cat-${t.category.id}">${t.category.name}</a>` : ""}
        <div class="seg feelings" role="group" aria-label="How you feel about this tag">${FEELINGS.map(([k, label, ic]) => html`<button type="button" class="${k === t.feeling ? `on feel-${k}` : ""}" data-act="feeling" data-id="${t.id}" data-feeling="${k}" aria-pressed="${k === t.feeling}">${ic ? icon(ic) : ""}${label}</button>`)}</div></div>
      ${t.description ? html`<p class="prose" style="margin:0">${t.description}</p>` : ""}
      ${t.aliases?.length ? html`<div class="note">Also called: ${t.aliases.join(", ")}</div>` : ""}
      <div class="note">Add more tags below to narrow it down, or leave some out.</div>
    </div>
    <div class="section" style="gap:8px" id="filter-head" hidden>
      <div class="page-title"><h1></h1></div>
      <div class="note">Scenes that match the filters below. <a href="/tag/${t.id}"><u>Back to just ${t.name}</u></a>, or keep them with Save search (under Filters).</div>
    </div>
    <div class="section" id="browser"></div>
  </div>`.s;
  const tagHead = $("#tag-head", view), filterHead = $("#filter-head", view);
  const onFilters = (words, f) => {
    const just = f.tags.length === 1 && f.tags[0] === t.id && !(f.q || f.not_tags.length || f.fav_tags || f.performers.length || f.studios.length || f.favorites);
    tagHead.hidden = !just;
    filterHead.hidden = just;
    $("h1", filterHead).textContent = words || "All scenes";
    document.title = just ? t.name : words || "Scenes";
  };
  await sceneBrowser($("#browser", view), ctx, { defaults: { tags: id, sort: "TRENDING", dir: "desc" }, onFilters });
}

async function viewTags(view, ctx) {
  const { categories } = await api("tags");
  if (!ctx.alive()) return;
  const GROUPS = [["ACTION", "Action"], ["SCENE", "Scene"], ["PEOPLE", "People"]];
  const byGroup = {};
  for (const c of categories) (byGroup[c.group] ||= []).push(c);
  view.innerHTML = html`<div class="page">
    <div class="page-title"><h1>Tags</h1><span class="sub">Click a tag to say how you feel about it; its magnifier opens its scenes, where you can add more tags to combine them.</span></div>
    ${[["love", "Tags you love"], ["like", "Tags you like"], ["dislike", "Tags you dislike"], ["hate", "Tags you hate"]].map(([k, title]) => {
      const ts = (status.tag_feelings || []).filter((t) => t.feeling === k);
      return ts.length ? html`<section class="section" style="gap:10px"><div class="section-head"><h2>${title}</h2>${k === "love" || (k === "like" && !status.tag_feelings.some((t) => t.feeling === "love")) ? html`<a class="more" href="/scenes?fav_tags=1">Scenes with any you love or like${icon("chevron")}</a>` : ""}</div>
      <div class="chips">${ts.map((t) => tagChip(t))}</div></section>` : "";
    })}
    <div class="filters">
      <div class="filter-row"><label class="field grow"><span>Find a tag</span><input class="input" id="t-q" type="search" placeholder="Tag name or alias" autocomplete="off"></label></div>
      <div class="chips" id="t-hits"></div>
    </div>
    ${GROUPS.filter(([g]) => byGroup[g]).map(([g, label]) => html`<section class="cat-group"><h2>${label}</h2><div class="cats">
      ${byGroup[g].sort((a, b) => a.name.localeCompare(b.name)).map((c) => html`<div class="cat" id="cat-${c.id}" data-cat="${c.id}"><button type="button" aria-expanded="false"><span><b>${c.name}</b>${c.description ? html`<small>${c.description}</small>` : ""}</span>${icon("chevron")}</button><div class="body" hidden></div></div>`)}
    </div></section>`)}
  </div>`.s;
  const hits = $("#t-hits", view);
  $("#t-q", view).addEventListener("input", debounce(async (e) => {
    const q = e.target.value.trim();
    if (q.length < 2) { hits.innerHTML = ""; return; }
    try {
      const { tags } = await api("tags" + qs({ q }));
      hits.innerHTML = tags.length ? tags.map((t) => tagChip(t, t.category ? html` <small style="color:var(--muted)">${t.category.name}</small>` : "").s).join("") : html`<span class="note">No tags match.</span>`.s;
    } catch (err) { hits.innerHTML = html`<span class="note bad-text">${err.message}</span>`.s; }
  }, 250));
  const open = async (cat) => {
    const btn = $("button", cat), body = $(".body", cat);
    const isOpen = cat.classList.toggle("open");
    btn.setAttribute("aria-expanded", isOpen);
    body.hidden = !isOpen;
    if (!isOpen || body.dataset.loaded) return;
    body.innerHTML = html`<div class="spinner"></div>`.s;
    try {
      let page = 1, all = [], count = 0;
      do {
        const res = await api("tags" + qs({ category: cat.dataset.cat, page }));
        all = all.concat(res.tags); count = res.count; page++;
      } while (all.length < count && page <= 10);
      body.dataset.loaded = "1";
      body.innerHTML = html`<div class="chips">${all.map((t) => tagChip(t))}</div>`.s;
    } catch (err) { body.innerHTML = html`<span class="note bad-text">${err.message}</span>`.s; }
  };
  $(".page", view).addEventListener("click", (e) => { const b = e.target.closest(".cat > button"); if (b) open(b.parentElement); });
  if (location.hash.startsWith("#cat-")) {
    const cat = $(location.hash, view);
    if (cat) { open(cat); cat.scrollIntoView({ block: "center" }); }
  }
}

function listBrowser(el, ctx, { endpoint, key, card, gridClass, controls, defaults }) {
  const params = new URLSearchParams(location.search);
  const f = { ...defaults };
  for (const k of Object.keys(defaults)) if (params.has(k)) f[k] = params.get(k);
  // Sorting isn't filtering: the button counts the rest that differ from the defaults.
  const activeCount = () => Object.keys(defaults).filter((k) => !["sort", "dir"].includes(k) && f[k] !== defaults[k]).length;
  el.innerHTML = html`<details class="filters">${filterSummary(activeCount())}<div class="filter-row">${controls(f)}</div></details>
    <div class="section-head"><h2 id="l-count" style="font-size:15px;color:var(--muted);font-weight:500"></h2></div>
    <div class="grid ${gridClass}" id="l-grid"></div><div class="load-more" id="l-more"></div>`.s;
  const grid = $("#l-grid", el), more = $("#l-more", el), countEl = $("#l-count", el);
  let page = 0, total = 0, shown = 0, loading = false, seq = 0, mem = null;
  const draw = (items) => {
    grid.insertAdjacentHTML("beforeend", items.map((x) => card(x).s).join(""));
    countEl.textContent = total ? `${total.toLocaleString()} found` : "";
    if (!total) grid.innerHTML = html`<div class="empty" style="grid-column:1/-1">Nothing matches.</div>`.s;
    more.innerHTML = shown < total ? html`<button class="btn" id="l-next">Load more</button>`.s : "";
  };
  const sync = () => {
    const changed = Object.fromEntries(Object.entries(f).filter(([k, v]) => v !== defaults[k]));
    history.replaceState(null, "", location.pathname + qs(changed));
    setFilterCount(el, activeCount());
    load(true);
  };
  async function load(reset) {
    const my = ++seq;
    if (reset) { page = 0; shown = 0; total = 0; grid.innerHTML = ""; mem = rememberList(); }
    loading = true;
    more.innerHTML = html`<div class="spinner"></div>`.s;
    try {
      const res = await api(endpoint + qs({ ...f, page: page + 1, per_page: 48 }));
      if (my !== seq || !ctx.alive()) return;
      page++;
      total = res.count;
      shown += res[key].length;
      draw(res[key]);
      mem.items.push(...res[key]);
      Object.assign(mem, { page, total, shown, at: Date.now() });
    } catch (e) {
      if (my === seq && ctx.alive()) more.innerHTML = errorBox(e).s;
    } finally { loading = false; }
  }
  el.addEventListener("input", debounce((e) => { if (e.target.matches("input[type=search]")) { f[e.target.name] = e.target.value.trim(); sync(); } }, 400));
  el.addEventListener("change", (e) => {
    if (e.target.matches("select")) { f[e.target.name] = e.target.value; sync(); }
    if (e.target.matches("input[type=checkbox]")) { f[e.target.name] = e.target.checked ? "1" : ""; sync(); }
  });
  more.addEventListener("click", (e) => { if (e.target.closest("#l-next")) load(false); });
  const io = new IntersectionObserver((en) => {
    if (!ctx.alive()) { io.disconnect(); return; }
    if (en[0].isIntersecting && !loading && shown && shown < total) load(false);
  }, { rootMargin: "600px" });
  io.observe(more);
  const old = recalledList(ctx);
  if (!old) return load(true);
  mem = old;
  ({ page, total, shown } = old);
  draw(old.items);
  restoreScroll(old.scroll);
}

const option = (v, label, cur) => html`<option value="${v}" ${v === cur ? "selected" : ""}>${label}</option>`;

async function viewPerformers(view, ctx) {
  view.innerHTML = html`<div class="page"><div class="page-title"><h1>Performers</h1></div><div class="section" id="list"></div></div>`.s;
  await listBrowser($("#list", view), ctx, {
    endpoint: "performers", key: "performers", card: personCard, gridClass: "people",
    defaults: { q: "", sort: "POPULARITY", gender: "FEMALE", favorites: "" },
    controls: (f) => html`
      <label class="field grow"><span>Name</span><input class="input" type="search" name="q" value="${f.q}" placeholder="Name or alias"></label>
      <label class="field"><span>Sort</span><select class="input" name="sort">${[["POPULARITY", "Most popular"], ["SCENE_COUNT", "Most scenes"], ["LAST_SCENE", "Latest scene"], ["DEBUT", "Newest debut"], ["CREATED_AT", "Newly added to StashDB"], ["NAME", "Name"]].map(([v, l]) => option(v, l, f.sort))}</select></label>
      <label class="field"><span>Gender</span><select class="input" name="gender">${[["ANY", "Any"], ["FEMALE", "Female"], ["MALE", "Male"], ["TRANSGENDER_FEMALE", "Trans female"], ["TRANSGENDER_MALE", "Trans male"], ["NON_BINARY", "Non-binary"], ["INTERSEX", "Intersex"]].map(([v, l]) => option(v, l, f.gender))}</select></label>
      <label class="check" style="align-self:end;height:38px"><input type="checkbox" name="favorites" ${f.favorites ? "checked" : ""}> StashDB favourites only</label>`,
  });
}

async function viewStudios(view, ctx) {
  view.innerHTML = html`<div class="page"><div class="page-title"><h1>Studios</h1></div><div class="section" id="list"></div></div>`.s;
  await listBrowser($("#list", view), ctx, {
    endpoint: "studios", key: "studios", card: studioCard, gridClass: "studios",
    defaults: { q: "", sort: "NAME", dir: "asc", favorites: "", top: "" },
    controls: (f) => html`
      <label class="field grow"><span>Name</span><input class="input" type="search" name="q" value="${f.q}" placeholder="Studio or network"></label>
      <label class="field"><span>Sort</span><select class="input" name="sort">${[["NAME", "Name"], ["CREATED_AT", "Newly added to StashDB"]].map(([v, l]) => option(v, l, f.sort))}</select></label>
      <label class="check" style="align-self:end;height:38px"><input type="checkbox" name="favorites" ${f.favorites ? "checked" : ""}> StashDB favourites only</label>
      <label class="check" style="align-self:end;height:38px"><input type="checkbox" name="top" ${f.top ? "checked" : ""}> Networks only</label>`,
  });
}

async function viewSearch(view, ctx) {
  const q = new URLSearchParams(location.search).get("q") || "";
  $("#search").value = q;
  if (!q) { view.innerHTML = html`<div class="page"><div class="empty">Type in the search bar to find scenes, performers, studios and tags.</div></div>`.s; return; }
  const r = await api("search" + qs({ q }));
  if (!ctx.alive()) return;
  const none = !r.scenes.length && !r.performers.length && !r.studios.length && !r.tags.length;
  view.innerHTML = html`<div class="page">
    <div class="page-title"><h1>Results for “${q}”</h1></div>
    ${none ? html`<div class="empty">Nothing on StashDB matches that.</div>` : ""}
    ${r.tags.length ? html`<section class="section"><div class="section-head"><h2>Tags</h2></div><div class="chips">${r.tags.map((t) => tagChip(t))}</div></section>` : ""}
    ${r.performers.length ? slider("Performers", r.performers, "people", `/performers${qs({ q })}`, personCard) : ""}
    ${r.studios.length ? slider("Studios", r.studios, "people", `/studios${qs({ q })}`, studioCard) : ""}
    ${r.scenes.length ? html`<section class="section"><div class="section-head"><h2>Scenes</h2><a class="more" href="/scenes${qs({ q })}">Search titles only${icon("chevron")}</a></div><div class="grid scenes">${r.scenes.map(sceneCard)}</div></section>` : ""}
  </div>`.s;
}

async function viewRequests(view, ctx) {
  const which = new URLSearchParams(location.search).get("filter") || (me.is_admin && status.pending ? "pending" : "all");
  const tabs = [["all", "All"], ["pending", "Pending"], ["approved", "Approved"], ["stalled", "Stalled"], ["available", "Available"], ["declined", "Declined"], ["failed", "Failed"]];
  view.innerHTML = html`<div class="page">
    <div class="page-title"><h1>Requests</h1>${!me.is_admin ? html`<span class="sub">Your requests</span>` : ""}</div>
    ${which === "stalled" ? html`<p class="note" style="margin:0">Approved, but no release found for a while: Whisparr only grabs a scene when a new release shows up, unless it's searched for again.</p>` : ""}
    <nav class="tabs">${tabs.map(([k, l]) => html`<a href="/requests?filter=${k}" class="${k === which ? "on" : ""}">${l}${k === "pending" && status.pending ? ` (${status.pending})` : ""}</a>`)}</nav>
    <div class="req-list" id="r-list"><div class="center"><div class="spinner"></div></div></div>
    <div class="load-more" id="r-more"></div>
  </div>`.s;
  const list = $("#r-list", view), more = $("#r-more", view);
  let page = 1, shown = 0;
  const load = async () => {
    const res = await api("requests" + qs({ filter: which, page }));
    if (!ctx.alive()) return;
    if (page === 1) list.innerHTML = "";
    list.insertAdjacentHTML("beforeend", res.requests.map((r) => requestRow(r).s).join(""));
    shown += 30;
    if (page === 1 && !res.requests.length) list.innerHTML = html`<div class="empty">${which === "stalled" ? "No stalled requests." : "No requests here. Find a scene and press Request."}</div>`.s;
    if (page === 1 && which === "stalled" && me.is_admin && res.count > 1) list.insertAdjacentHTML("afterbegin", html`<div class="btns"><button class="btn" data-act="search-all">${icon("refresh")}Search again for all ${res.count}</button></div>`.s);
    more.innerHTML = shown < res.count ? html`<button class="btn" id="r-next">Load more</button>`.s : "";
  };
  more.addEventListener("click", (e) => { if (e.target.closest("#r-next")) { page++; load().catch((err) => toast(err.message, true)); } });
  await load();
}

async function viewSaved(view, ctx) {
  const { watches } = await api("watches");
  if (!ctx.alive()) return;
  const canAuto = me.is_admin || me.auto_approve;
  const row = (w) => html`<div class="watch ${w.enabled ? "" : "off"}" data-watch="${w.id}">
    <div class="info">
      <div class="t">${w.name}</div>
      ${w.summary && w.summary !== w.name ? html`<div class="m">${w.summary}</div>` : ""}
      <div class="m">Scenes released from ${fmtDate(w.since)} · ${w.requests} requested${w.waiting ? ` · ${w.waiting} not in Whisparr yet` : ""}${w.user_id !== me.id ? ` · ${w.owner}'s` : ""}</div>
      <div class="m">${!w.enabled ? "Paused" : w.checked ? `Checked ${fmtAgo(w.checked)}` : "Checked within the hour"}${w.ask_first || !canAuto ? " · requests wait for approval" : ""}</div>
      ${w.error ? html`<div class="err">${w.error}</div>` : ""}
    </div>
    <div class="btns">
      ${canAuto ? html`<label class="check"><input type="checkbox" data-wact="ask" ${w.ask_first ? "checked" : ""}> Ask first</label>` : ""}
      <a class="btn small ghost" href="/scenes?${w.query}">Show scenes</a>
      <button class="btn small" data-wact="check">${icon("refresh")}Check now</button>
      <button class="btn small ghost" data-wact="toggle">${w.enabled ? "Pause" : "Resume"}</button>
      <button class="btn small danger" data-wact="del">Delete</button>
    </div>
  </div>`;
  view.innerHTML = html`<div class="page">
    <div class="page-title"><h1>Saved searches</h1><span class="sub">New scenes that match are requested every hour</span></div>
    ${watches.length ? html`<div class="watch-list">${watches.map(row)}</div>`
      : html`<div class="empty">No saved searches yet. On Scenes, or a tag's, performer's or studio's page, open Filters, pick what you want and press Save search.</div>`}
  </div>`.s;
  const put = (id, body) => api(`watches/${id}`, { method: "PUT", body });
  // On this render's page: #view stays, and its listeners would pile up across visits.
  const page = $(".page", view);
  page.addEventListener("change", async (e) => {
    const box = e.target.closest("[data-wact=ask]");
    if (!box) return;
    try { await put(box.closest("[data-watch]").dataset.watch, { ask_first: box.checked }); toast(box.checked ? "Its requests wait for your approval." : "Its requests go straight to Whisparr."); }
    catch (err) { toast(err.message, true); render(); }
  });
  page.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-wact]");
    if (!b || b.classList.contains("busy")) return;
    const id = b.closest("[data-watch]").dataset.watch;
    const w = watches.find((x) => String(x.id) === id);
    if (b.dataset.wact === "del" && b.dataset.sure !== "1") {
      b.dataset.sure = "1"; b.textContent = "Delete it?";
      setTimeout(() => { if (b.isConnected) { b.dataset.sure = ""; b.textContent = "Delete"; } }, 4000);
      return;
    }
    b.classList.add("busy");
    try {
      if (b.dataset.wact === "check") {
        const r = await api(`watches/${id}/check`, { method: "POST" });
        toast(`${r.found} new match${r.found === 1 ? "" : "es"}, ${r.requested} requested${r.waiting ? `, ${r.waiting} not in Whisparr yet` : ""}.`);
      } else if (b.dataset.wact === "toggle") {
        await put(id, { enabled: !w.enabled });
      } else if (b.dataset.wact === "del") {
        await api(`watches/${id}`, { method: "DELETE" });
        toast(`Deleted “${w.name}”. Its requests stay.`);
      }
      render();
    } catch (err) { toast(err.message, true); b.classList.remove("busy"); }
  });
}

const NOTIFY_EVENTS = [
  ["pending", "A request waits for approval"],
  ["watch", "A saved search requests a scene"],
  ["auto", "A request is approved automatically (admins' own and auto-approved users')"],
  ["approved", "An admin approves a request"],
  ["declined", "An admin declines a request"],
  ["available", "A requested scene is downloaded"],
  ["failed", "A request fails"],
  ["stalled", "A request finds no release for too long (set under Whisparr)"],
];

async function viewSettings(view, ctx) {
  const parts = [];
  let s = null, users = null;
  if (me.is_admin) {
    [s, { users }] = await Promise.all([api("settings"), api("users")]);
    if (!ctx.alive()) return;
  }
  const account = html`<section class="panel" id="p-account">
    <h2>Your password</h2>
    <form class="form-grid" id="pw-form">
      <label class="field"><span>Current password</span><input class="input" type="password" name="current" autocomplete="current-password" required></label>
      <label class="field"><span>New password</span><input class="input" type="password" name="new" autocomplete="new-password" minlength="8" required></label>
      <div class="btns" style="align-self:end"><button class="btn primary">Change password</button></div>
    </form>
  </section>`;
  const privacy = html`<section class="panel">
    <h2>On this device</h2>
    <label class="check"><input type="checkbox" id="pv-covers" ${document.body.classList.contains("hide-covers") ? "checked" : ""}> Blur covers until I point at them (the eye button in the top bar does the same)</label>
  </section>`;
  if (me.is_admin) {
    parts.push(html`<section class="panel">
      <h2>StashDB</h2>
      <p class="lede">Scenes, performers, studios and tags come from StashDB. The API key is on your stashdb.org profile page; your StashDB favourites show up on Discover.</p>
      <form class="form-grid" id="sd-form">
        <label class="field" style="grid-column:1/-1"><span>API key</span><input class="input" name="stashdb_key" type="password" autocomplete="off" placeholder="${s.stashdb_key_set ? "Saved. Paste a new one to replace it." : "Paste your StashDB API key"}"></label>
        <div class="btns"><button class="btn primary">Save key</button><span class="note ${s.stashdb_key_set ? "ok-text" : "bad-text"}">${s.stashdb_key_set ? "A key is saved." : "No key yet: browsing won't work until you add one."}</span></div>
      </form>
    </section>`);
    parts.push(html`<section class="panel">
      <h2>Whisparr</h2>
      <p class="lede">Approved requests are added to Whisparr and searched for; following a performer or studio monitors their new scenes there.</p>
      ${s.whisparr_error ? html`<div class="error-box">${s.whisparr_error}</div>` : ""}
      <form class="form-grid" id="wh-form">
        <label class="field"><span>Address</span><input class="input" name="whisparr_url" value="${s.whisparr_url}" placeholder="http://localhost:6969"></label>
        <label class="field"><span>API key</span><input class="input" name="whisparr_key" type="password" autocomplete="off" placeholder="${s.whisparr_key_from_config ? "Read from torrent-box's Whisparr" : s.whisparr_key_set ? "Saved" : "Whisparr → Settings → General"}"></label>
        <label class="field"><span>Quality profile</span><select class="input" name="quality_profile_id">${option("", "Same as most studios", s.quality_profile_id)}${(s.profiles || []).map((p) => option(String(p.id), p.name, s.quality_profile_id))}</select></label>
        <label class="field"><span>Folder for new scenes</span><select class="input" name="root_folder">${option("", "Whisparr's first folder", s.root_folder)}${(s.roots || []).map((r) => option(r.path, r.path, s.root_folder))}</select></label>
        <label class="field"><span>Stalled after (days without a release)</span><input class="input" type="number" name="stall_days" min="1" max="365" value="${s.stall_days}"></label>
        <label class="check" style="grid-column:1/-1"><input type="checkbox" name="search_on_request" ${s.search_on_request ? "checked" : ""}> Search for a scene as soon as its request is approved. Without this, Whisparr only grabs it if it turns up in the RSS feed.</label>
        <div class="btns"><button class="btn primary">Save</button></div>
      </form>
    </section>`);
    const tb = s.torrent_box_ntfy;
    parts.push(html`<section class="panel">
      <h2>Notifications</h2>
      <p class="lede">Pushes through ntfy, like Seerr's.${tb ? " Left empty, they go to torrent-box's ntfy topic, so the phone that gets its pushes gets these too." : " Subscribe to the topic in the ntfy app."}</p>
      ${s.notify_error ? html`<div class="error-box">${s.notify_error}</div>` : ""}
      <form class="form-grid" id="nt-form">
        <label class="field"><span>ntfy server</span><input class="input" name="notify_server" value="${s.notify_server}" placeholder="${tb ? `torrent-box's: ${tb}` : "https://ntfy.sh"}"></label>
        <label class="field"><span>Topic</span><input class="input" name="notify_topic" value="${s.notify_topic}" autocomplete="off" placeholder="${tb ? "torrent-box's topic" : "A long, random name"}"></label>
        <label class="field" style="grid-column:1/-1"><span>Desireerr's address, for links in pushes</span><input class="input" name="app_url" value="${s.app_url}" placeholder="${s.seen_origin || location.origin}"></label>
        <fieldset class="events"><legend>Push when</legend>${NOTIFY_EVENTS.map(([k, l]) => html`<label class="check"><input type="checkbox" name="ev" value="${k}" ${s.notify_events.includes(k) ? "checked" : ""}> ${l}</label>`)}</fieldset>
        <label class="check" style="grid-column:1/-1"><input type="checkbox" name="notify_discreet" ${s.notify_discreet ? "checked" : ""}> Discreet: no titles, names or covers, only “${s.tab_title || "Desireerr"}: A request is ready.” and the like. Without it, pushes are like Seerr's: what happened, the scene, who asked, its cover and a link to it.</label>
        <div class="btns"><button class="btn primary">Save</button><button class="btn ghost" type="button" id="nt-test">Save and send a test</button></div>
      </form>
    </section>`);
    parts.push(html`<section class="panel">
      <h2>Jellyfin</h2>
      <p class="lede">With an API key, downloaded scenes get a Watch button that opens them in Jellyfin. On a torrent-box server, its setup makes the key.</p>
      ${s.jellyfin_error ? html`<div class="error-box">${s.jellyfin_error}</div>` : ""}
      ${s.nfo_error ? html`<div class="error-box">Writing .nfo files: ${s.nfo_error}</div>` : ""}
      <form class="form-grid" id="jf-form">
        <label class="field"><span>Address (from this server)</span><input class="input" name="jellyfin_url" value="${s.jellyfin_url}" placeholder="http://localhost:8096"></label>
        <label class="field"><span>API key</span><input class="input" name="jellyfin_key" type="password" autocomplete="off" placeholder="${s.jellyfin_key_set ? "Saved. Paste a new one to replace it." : "Jellyfin: Dashboard, API Keys"}"></label>
        <label class="field" style="grid-column:1/-1"><span>Address for the Watch button (from your browser)</span><input class="input" name="jellyfin_link" value="${s.jellyfin_link}" placeholder="${location.protocol}//${location.hostname}:8096"></label>
        <label class="check" style="grid-column:1/-1"><input type="checkbox" name="write_nfo" ${s.write_nfo ? "checked" : ""}> Write the downloaded scenes' .nfo files and covers (folder.jpg) from StashDB: the performers as the cast, with their photos, and the studio, tags and description. Whisparr's own metadata is turned off meanwhile, and back on without this.</label>
        <div class="btns"><button class="btn primary">Save</button>${s.jellyfin_key_set && !s.jellyfin_error ? html`<span class="note ok-text">Connected: Jellyfin has ${s.jellyfin_videos} videos.</span>` : ""}</div>
      </form>
    </section>`);
    parts.push(html`<section class="panel">
      <h2>Privacy</h2>
      <form class="form-grid" id="pr-form">
        <label class="field"><span>Browser tab title</span><input class="input" name="tab_title" value="${s.tab_title}" maxlength="40"></label>
        <div class="btns" style="align-self:end"><button class="btn primary">Save</button></div>
      </form>
      <p class="lede" style="margin:0">Shown in tabs, bookmarks and the sign-in page. Set something neutral if others see your screen.</p>
    </section>`);
    parts.push(html`<section class="panel" style="max-width:none">
      <h2>Users</h2>
      <p class="lede">Admins approve requests and change settings. Auto-approved users' requests go straight to Whisparr; everyone else's wait for an admin.</p>
      <div class="table-wrap"><table><thead><tr><th>User</th><th>Role</th><th>Auto-approve</th><th class="num">Requests</th><th>Added</th><th></th></tr></thead><tbody>
        ${users.map((u) => html`<tr data-user="${u.id}">
          <td><b>${u.username}</b>${u.id === me.id ? html` <span class="note">(you)</span>` : ""}</td>
          <td><select class="input" data-field="is_admin" ${u.id === me.id ? "disabled" : ""}>${option("1", "Admin", String(u.is_admin))}${option("0", "User", String(u.is_admin))}</select></td>
          <td><input type="checkbox" data-field="auto_approve" ${u.auto_approve || u.is_admin ? "checked" : ""} ${u.is_admin ? "disabled" : ""} aria-label="Auto-approve"></td>
          <td class="num">${u.requests}</td>
          <td>${new Date(u.created * 1000).toLocaleDateString()}</td>
          <td><div class="btns"><button class="btn small ghost" data-uact="pw">Set password</button>${u.id !== me.id ? html`<button class="btn small danger" data-uact="del">Delete</button>` : ""}</div></td>
        </tr>`)}
      </tbody></table></div>
      <form class="form-grid" id="u-form">
        <label class="field"><span>New user's name</span><input class="input" name="username" autocomplete="off" required></label>
        <label class="field"><span>Password</span><input class="input" name="password" type="password" autocomplete="new-password" minlength="8" required></label>
        <label class="check" style="align-self:end;height:38px"><input type="checkbox" name="auto_approve"> Auto-approve</label>
        <label class="check" style="align-self:end;height:38px"><input type="checkbox" name="is_admin"> Admin</label>
        <div class="btns"><button class="btn primary">${icon("plus")}Add user</button></div>
      </form>
    </section>`);
  }
  view.innerHTML = html`<div class="page"><div class="page-title"><h1>Settings</h1><span class="sub">Desireerr ${status.version}</span></div>${parts}${account}${privacy}</div>`.s;

  const formData = (form) => Object.fromEntries(new FormData(form).entries());
  const submit = (id, fn) => {
    const form = $(id, view);
    if (!form) return;
    form.onsubmit = async (e) => {
      e.preventDefault();
      const btn = $("button.primary", form);
      btn.classList.add("busy");
      try { await fn(form); } catch (err) { toast(err.message, true); } finally { btn.classList.remove("busy"); }
    };
  };
  submit("#sd-form", async (form) => {
    const key = form.stashdb_key.value.trim();
    if (!key) return toast("Paste a key first.", true);
    await api("settings", { method: "PUT", body: { stashdb_key: key } });
    toast("StashDB accepted the key. Saved.");
    render();
  });
  submit("#wh-form", async (form) => {
    const d = formData(form);
    const body = { whisparr_url: d.whisparr_url, quality_profile_id: d.quality_profile_id, root_folder: d.root_folder, search_on_request: form.search_on_request.checked, stall_days: d.stall_days };
    if (d.whisparr_key) body.whisparr_key = d.whisparr_key;
    const res = await api("settings", { method: "PUT", body });
    toast(res.whisparr_error ? `Saved, but: ${res.whisparr_error}` : "Saved.", !!res.whisparr_error);
    render();
  });
  const saveNotify = (form) => api("settings", { method: "PUT", body: {
    notify_server: form.notify_server.value, notify_topic: form.notify_topic.value, app_url: form.app_url.value,
    notify_events: $$("input[name=ev]:checked", form).map((x) => x.value), notify_discreet: form.notify_discreet.checked,
  } });
  submit("#nt-form", async (form) => { await saveNotify(form); toast("Saved."); });
  if ($("#nt-test", view)) $("#nt-test", view).onclick = async (e) => {
    const b = e.currentTarget;
    b.classList.add("busy");
    try { await saveNotify($("#nt-form", view)); await api("notify-test", { method: "POST" }); toast("Sent. It should be on your phone now."); }
    catch (err) { toast(err.message, true); } finally { b.classList.remove("busy"); }
  };
  submit("#jf-form", async (form) => {
    const body = { jellyfin_url: form.jellyfin_url.value, jellyfin_link: form.jellyfin_link.value, write_nfo: form.write_nfo.checked };
    if (form.jellyfin_key.value.trim()) body.jellyfin_key = form.jellyfin_key.value.trim();
    const res = await api("settings", { method: "PUT", body });
    toast(res.jellyfin_error ? `Saved, but: ${res.jellyfin_error}` : "Saved.", !!res.jellyfin_error);
    await refreshStatus();
    render();
  });
  submit("#pr-form", async (form) => {
    await api("settings", { method: "PUT", body: { tab_title: form.tab_title.value } });
    document.title = form.tab_title.value || "Desireerr";
    toast("Saved.");
  });
  submit("#u-form", async (form) => {
    const d = formData(form);
    await api("users", { method: "POST", body: { username: d.username, password: d.password, auto_approve: form.auto_approve.checked, is_admin: form.is_admin.checked } });
    toast(`Added ${d.username}.`);
    render();
  });
  submit("#pw-form", async (form) => {
    await api("password", { method: "POST", body: { current: form.current.value, new: form.new.value } });
    form.reset();
    toast("Password changed. Other devices are signed out.");
  });
  $("#pv-covers", view).onchange = (e) => setCovers(e.target.checked);
  // On this render's page: listeners on #view itself would pile up across visits (and a
  // second one would delete a user without asking).
  const page = $(".page", view);
  page.addEventListener("change", async (e) => {
    const field = e.target.dataset.field;
    const row = e.target.closest("[data-user]");
    if (!field || !row) return;
    const value = field === "is_admin" ? e.target.value === "1" : e.target.checked;
    try { await api(`users/${row.dataset.user}`, { method: "PUT", body: { [field]: value } }); toast("Saved."); if (field === "is_admin") render(); }
    catch (err) { toast(err.message, true); render(); }
  });
  page.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-uact]");
    if (!b) return;
    const row = b.closest("[data-user]");
    const name = $("b", row).textContent;
    if (b.dataset.uact === "del") {
      if (b.dataset.sure !== "1") { b.dataset.sure = "1"; b.textContent = `Delete ${name}?`; setTimeout(() => { if (b.isConnected) { b.dataset.sure = ""; b.textContent = "Delete"; } }, 4000); return; }
      try { await api(`users/${row.dataset.user}`, { method: "DELETE" }); toast(`Deleted ${name}.`); render(); } catch (err) { toast(err.message, true); }
    } else if (b.dataset.uact === "pw") {
      const cell = b.parentElement;
      cell.innerHTML = html`<form class="btns" data-pwform><input class="input" type="password" minlength="8" placeholder="New password" required autocomplete="new-password" style="width:160px"><button class="btn small primary">Set</button></form>`.s;
      const f = $("form", cell);
      $("input", f).focus();
      f.onsubmit = async (ev) => {
        ev.preventDefault();
        try { await api(`users/${row.dataset.user}`, { method: "PUT", body: { password: $("input", f).value } }); toast(`New password set for ${name}.`); render(); }
        catch (err) { toast(err.message, true); }
      };
    }
  });
}

// ---------------------------------------------------------------------------------------
// Sign-in and first-run setup
// ---------------------------------------------------------------------------------------

const brandBlock = html`<div class="logo"><img src="/static/logo.svg" alt="" width="72" height="72"><div class="wordmark">Desir<b>eerr</b></div></div>`;

function viewLogin() {
  if (me) return go("/", true);
  root.innerHTML = html`<div class="auth"><form class="auth-card" id="login">
    ${brandBlock}
    <label class="field"><span>Username</span><input class="input" name="username" autocomplete="username" required autofocus></label>
    <label class="field"><span>Password</span><input class="input" name="password" type="password" autocomplete="current-password" required></label>
    <div class="error-box" id="err" hidden></div>
    <button class="btn primary">Sign in</button>
  </form></div>`.s;
  $("#login").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target, err = $("#err");
    err.hidden = true;
    $("button", f).classList.add("busy");
    try {
      await api("login", { method: "POST", body: { username: f.username.value, password: f.password.value } });
      await refreshStatus();
      const next = new URLSearchParams(location.search).get("next");
      go(next && next.startsWith("/") && !next.startsWith("//") ? next : "/", true);
    } catch (ex) {
      err.textContent = ex.message; err.hidden = false;
      f.password.select();
    } finally { $("button", f).classList.remove("busy"); }
  };
}

function viewSetup() {
  if (!status.setup_needed) return go("/", true);
  root.innerHTML = html`<div class="auth"><form class="auth-card wide" id="setup">
    ${brandBlock}
    <p class="lede">Create the admin account. Nothing in Desireerr opens without signing in, covers included.</p>
    <label class="field"><span>Setup code</span><input class="input" name="setup_code" autocomplete="off" required placeholder="8 characters"></label>
    <p class="note" style="margin:-6px 0 0">So nobody else on your network can claim the app first. On the server: <code>cat ~/.local/state/desireerr/setup-code</code></p>
    <label class="field"><span>Username</span><input class="input" name="username" autocomplete="username" required autofocus></label>
    <label class="field"><span>Password (at least 8 characters)</span><input class="input" name="password" type="password" autocomplete="new-password" minlength="8" required></label>
    <label class="field"><span>Password again</span><input class="input" name="password2" type="password" autocomplete="new-password" minlength="8" required></label>
    <label class="field"><span>StashDB API key</span><input class="input" name="stashdb_key" type="password" autocomplete="off" placeholder="From your stashdb.org profile page"></label>
    <p class="note" style="margin:-6px 0 0">You can add the key later in Settings. Whisparr is found on its own on a torrent-box server.</p>
    <div class="error-box" id="err" hidden></div>
    <button class="btn primary">Create admin and sign in</button>
  </form></div>`.s;
  $("#setup").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target, err = $("#err");
    err.hidden = true;
    if (f.password.value !== f.password2.value) { err.textContent = "The two passwords differ."; err.hidden = false; return; }
    $("button", f).classList.add("busy");
    try {
      await api("setup", { method: "POST", body: { setup_code: f.setup_code.value, username: f.username.value, password: f.password.value, stashdb_key: f.stashdb_key.value.trim() } });
      await refreshStatus();
      go("/", true);
    } catch (ex) {
      err.textContent = ex.message; err.hidden = false;
    } finally { $("button", f).classList.remove("busy"); }
  };
}

render();
