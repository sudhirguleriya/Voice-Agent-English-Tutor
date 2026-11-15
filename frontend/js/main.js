// Entry point: routing, practice topic picker, keyboard shortcuts, sidebar state.
import { showFocus } from "./focus.js";
import { endSession, pauseResume, primary, render, resetPanels, session } from "./session.js";
import { openSettings } from "./settings.js";
import { showSessions } from "./sessions.js";
import { $, $$, api, esc, ICON, showError } from "./util.js";

let view = "live";
let topic = "";

// ---------------------------------------------------------------- routing: #live #practice?topic=… #sessions/12 #focus
function route() {
  const [path, query] = (location.hash.slice(1) || "live").split("?");
  const [name, arg] = path.split("/");
  view = ["live", "practice", "sessions", "focus"].includes(name) ? name : "live";
  $$("[data-nav]").forEach((a) => { if (a.dataset.nav === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  $$(".view").forEach((v) => (v.hidden = v.id !== `view-${view}`));
  if (view === "practice") {
    const t = new URLSearchParams(query || "").get("topic");
    if (t && session.state === "idle") { topic = t; $("#ownTopic").value = t; markChips(); }
  }
  if ((view === "live" || view === "practice") && session.state === "idle") { session.mode = view; resetPanels(); render(); }
  if (view === "sessions") showSessions(arg ? +arg : null);
  if (view === "focus") showFocus();
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- controls
function currentTopic() { return ($("#ownTopic").value.trim() || topic || "Small talk: my day so far"); }

function onPrimary(mode) {
  if (session.state !== "idle" && session.mode !== mode) {
    showError(`Finish the current ${session.mode} session first (End & review).`);
    return;
  }
  if (mode === "practice" && session.state === "idle") $("#prTopic").textContent = "Topic: " + currentTopic();
  primary(mode, mode === "practice" ? currentTopic() : null);
}
$("#liveMic").onclick = () => onPrimary("live");
$("#prMic").onclick = () => onPrimary("practice");
$("#livePause").onclick = $("#prPause").onclick = () => pauseResume();
$("#liveEnd").onclick = $("#prEnd").onclick = () => endSession();

session.onSummary = (id) => { location.hash = `#sessions/${id}`; };

// sidebar: microphone card + red dot on the active screen
session.onState = (s) => {
  const card = $("#micState");
  if (s.micName) $("#micName").textContent = s.micName;
  if (s.micError) { card.className = "mic-state err"; card.lastElementChild.textContent = "Blocked: allow the mic"; return; }
  if (!s.state) return;
  const live = s.state === "running", paused = s.state === "paused";
  card.className = "mic-state " + (live ? "rec" : paused ? "" : "ok");
  card.lastElementChild.textContent = live ? "Listening" : paused ? "Paused" : "Ready";
  $$("[data-rec]").forEach((d) => (d.hidden = !((live || paused) && d.dataset.rec === s.mode)));
  $("#topicCard").classList.toggle("locked", s.mode === "practice" && s.state !== "idle");
};

// Space = start / pause / resume, E = end & review (not while typing)
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea, select, dialog, [contenteditable]") || e.ctrlKey || e.metaKey || e.altKey) return;
  const onSessionScreen = view === "live" || view === "practice";
  if (e.code === "Space" && onSessionScreen) {
    e.preventDefault();
    if (e.target.closest("button")) e.target.blur();
    onPrimary(view);
  } else if (e.key.toLowerCase() === "e" && (session.state === "running" || session.state === "paused")) {
    endSession();
  }
});

// ---------------------------------------------------------------- practice topics
async function loadTopics() {
  try {
    const { topics } = await api("/api/topics");
    $("#topicChips").innerHTML = topics.map((t) => `<button type="button" class="chip" aria-pressed="false" data-topic="${esc(t)}">${esc(t)}</button>`).join("");
    if (!topic) topic = topics[0];
    markChips();
  } catch { /* the page still works without the list */ }
  try {
    const f = await api("/api/focus");
    const areas = (f.run?.focus_areas || []).slice(0, 3);
    $("#steer").innerHTML = areas.length
      ? `${ICON.target} Steering toward your focus areas: ${areas.map((a) => `<span class="tag indigo">${esc(a.title)}</span>`).join("")}`
      : `${ICON.target} <span class="sub">Your partner will steer toward your focus areas once they're analysed.</span>`;
  } catch {}
}
$("#topicChips").onclick = (e) => {
  const c = e.target.closest("[data-topic]");
  if (!c || session.state !== "idle") return;
  topic = c.dataset.topic;
  $("#ownTopic").value = "";
  markChips();
};
$("#ownTopic").oninput = () => markChips();
function markChips() {
  const own = $("#ownTopic").value.trim();
  if (session.state === "idle") $("#prTopic").textContent = "Topic: " + (own || topic || "choose one above");
  $$("#topicChips .chip").forEach((c) => {
    const on = !own && c.dataset.topic === topic;
    c.setAttribute("aria-pressed", String(on));
    c.innerHTML = (on ? ICON.check(14) : "") + esc(c.dataset.topic);
  });
}

// ---------------------------------------------------------------- misc
$("#banner").onclick = () => ($("#banner").hidden = true);
$("#settingsBtn").onclick = () => openSettings().catch((e) => showError(e.message));
api("/api/health").then((h) => { if (!h.key_configured) showError("No API key on the server. Add it to .env and restart."); }).catch((e) => showError(e.message));
loadTopics();
route();
render();
