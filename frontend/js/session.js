// Live and Practice sessions: mic -> WebSocket -> transcript, Suggestions, Mistakes.
// Both screens share this controller; `UI[mode]` maps it to that screen's elements.
import { Mic, Partials } from "./mic.js";
import { attachMic, playReply, sayBtn, stopAudio } from "./speech.js";
import { $, api, esc, fixHtml, fmtClock, highlight, ICON, cap, showError } from "./util.js";

const HINT_PAUSE_MS = 700, HINT_MAX_GAP_MS = 2200;
const ids = (p) => ({
  transcript: `#${p}Transcript`, suggestions: `#${p}Suggestions`, mistakes: `#${p}Mistakes`, count: `#${p}Count`,
  wave: `#${p}Wave`, timer: `#${p}Timer`, status: `#${p}Status`, sub: `#${p}Sub`, mic: `#${p}Mic`,
  pause: `#${p}Pause`, end: `#${p}End`,
});
const UI = { live: ids("live"), practice: ids("pr") };
const el = (key) => $(UI[session.mode][key]);

export const session = {
  mode: "live", state: "idle",          // idle | connecting | running | paused | finishing
  ws: null, mic: null, partials: null, id: null, topic: null,
  elapsed: 0, resumedAt: 0, timer: null,
  lines: new Map(), levels: [],
  hint: { text: "", sent: "", last: 0, timer: null },
  onSummary: null, onState: null,       // set by main.js
};
const running = () => session.state === "running" || session.state === "paused";

// ---------------------------------------------------------------- lifecycle
export async function startSession(mode, topic) {
  if (session.state !== "idle") return;
  Object.assign(session, { mode, topic, state: "connecting", id: null, elapsed: 0, lines: new Map() });
  resetPanels();
  render();
  setStatus(mode === "practice" ? "Connecting to your partner…" : "Connecting…", "One moment");

  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/session`);
  session.ws = ws;
  ws.onmessage = (e) => onMessage(JSON.parse(e.data));
  ws.onclose = () => { if (running() || session.state === "connecting") { showError("Lost connection to the server."); teardown(); } };
  const opened = await new Promise((res) => { ws.onopen = () => res(true); setTimeout(() => res(false), 8000); });
  if (!opened) { showError("Couldn't connect to the Voice Coach server."); teardown(); return; }
  ws.send(JSON.stringify({ type: "start", mode, topic }));

  session.mic = new Mic({
    onSegment: (wav, ms) => sendSegment(wav, ms),
    onLevel: (v) => pushLevel(v),
    onSpeaking: (on) => {
      el("mic").classList.toggle("hearing", on);
      if (on) setStatus("Hearing you…");
      else { session.partials?.sentenceDone(); idleStatus(); }
    },
  });
  attachMic(session.mic);
  if (!(await openMic())) { ws.close(); teardown(); return; }
  session.state = "running";
  session.resumedAt = Date.now();
  session.timer = setInterval(tick, 500);
  render();
  idleStatus();
}

async function openMic() {
  try {
    await session.mic.start();
    const label = session.mic.stream?.getAudioTracks()[0]?.label;
    session.onState?.({ micName: label || "Default microphone" });
  } catch (e) {
    showError("Microphone blocked: " + e.message + ". Allow the mic for this page and try again.");
    session.onState?.({ micError: true });
    return false;
  }
  session.partials = new Partials(onPartial);
  session.partials.start();
  return true;
}

export async function pauseResume() {
  if (session.state === "running") {
    session.mic.stop();                     // flushes the sentence in progress
    session.partials?.stop();
    session.elapsed += Date.now() - session.resumedAt;
    session.state = "paused";
    pushLevel(0, true);
    setStatus("Paused", "Press Resume or Space to continue");
  } else if (session.state === "paused") {
    if (!(await openMic())) return;
    session.resumedAt = Date.now();
    session.state = "running";
    idleStatus();
  }
  render();
}

export function endSession() {
  if (!running()) return;
  const wasRunning = session.state === "running";
  session.state = "finishing";
  stopAudio();
  if (wasRunning) session.mic?.stop();
  session.partials?.stop();
  clearInterval(session.timer);
  setStatus("Writing your review…", "This takes a few seconds");
  render();
  try { session.ws.send(JSON.stringify({ type: "stop" })); } catch { teardown(); }
  setTimeout(() => { if (session.state === "finishing") { const id = session.id; teardown(); if (id) session.onSummary?.(id); } }, 90000);
}

// the one button: start when idle, pause/resume while running
export function primary(mode, topic) {
  if (session.state === "idle") return startSession(mode, topic);
  if (session.mode === mode && running()) return pauseResume();
}

function teardown() {
  try { session.mic?.stop(); } catch {}
  session.partials?.stop();
  clearInterval(session.timer);
  try { session.ws?.close(); } catch {}
  Object.assign(session, { state: "idle", ws: null, mic: null, partials: null });
  attachMic(null);
  pushLevel(0, true);
  render();
  idleStatus();
}

function sendSegment(wav, ms) {
  const ws = session.ws;
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "segment", duration_ms: ms }));
  ws.send(wav); // binary frame right after its header
  showPending();
}

const now = () => session.elapsed + (session.state === "running" ? Date.now() - session.resumedAt : 0);
function tick() { el("timer").textContent = fmtClock(now()); }

// ---------------------------------------------------------------- server messages
function onMessage(m) {
  switch (m.type) {
    case "session": session.id = m.session_id; break;
    case "utterance": addLine(m.utterance); break;
    case "analysis": applyAnalysis(m); break;
    case "hints": showHints(m); break;
    case "agent":
      addLine(m.utterance);
      setStatus("Your partner is speaking", "Listen, then answer");
      playReply(m.utterance.text, m.audio, m.mime).then(() => {
        session.partials?.sentenceDone(); // forget the partner's words the browser recognizer heard
        idleStatus();
      });
      break;
    case "status":
      if (m.state === "transcribing") setStatus("Writing it down…");
      else if (m.state === "agent_thinking") setStatus("Your partner is thinking…", "One moment");
      else if (m.state === "idle") idleStatus();
      break;
    case "skipped": removePending(); break;
    case "summary": { const id = m.summary.id; teardown(); session.onSummary?.(id); break; }
    case "error": showError(m.message); removePending(); break;
  }
}

// ---------------------------------------------------------------- status + controls
function setStatus(main, sub) {
  el("status").textContent = main;
  if (sub !== undefined) el("sub").textContent = sub;
}

function idleStatus() {
  const p = session.mode === "practice";
  if (session.state === "running") setStatus(p ? "Your turn" : "Listening", p ? "Speak when you're ready" : "Real conversation · English");
  else if (session.state === "paused") setStatus("Paused", "Press Resume or Space to continue");
  else if (session.state === "idle") setStatus(p ? "Ready when you are" : "Ready to listen", p ? "Pick a topic, then press the mic or Space" : "Press the mic or Space to start");
}

export function render() {
  for (const mode of ["live", "practice"]) {
    const u = UI[mode], mine = session.mode === mode, st = mine ? session.state : "idle";
    const mic = $(u.mic), pause = $(u.pause), end = $(u.end);
    mic.classList.toggle("on", st === "running");
    mic.classList.toggle("paused", st === "paused");
    mic.setAttribute("aria-label", st === "idle" ? (mode === "practice" ? "Start conversation" : "Start listening") : st === "paused" ? "Resume" : "Pause");
    mic.disabled = st === "connecting" || st === "finishing";
    pause.hidden = end.hidden = !(st === "running" || st === "paused");
    pause.innerHTML = st === "paused" ? `${ICON.play}<span>Resume</span>` : `${ICON.pause}<span>Pause</span>`;
    end.disabled = st === "finishing";
    const dot = mode === "live" ? $("#liveDot") : null;
    if (dot) dot.className = "dot" + (st === "running" ? " live" : st === "paused" ? " paused" : "");
    if (mode === "practice") $("#prPill").hidden = !(st === "running" || st === "paused");
    if (!mine || st === "idle") $(u.timer).textContent = mine ? fmtClock(session.elapsed) : "00:00";
  }
  session.onState?.({ state: session.state, mode: session.mode });
}

// waveform: the newest bars on the left are the live level, the tail fades
function pushLevel(v, reset = false) {
  const n = session.mode === "practice" ? 24 : 30;
  if (reset) session.levels = new Array(n).fill(0);
  else { session.levels.unshift(v); session.levels.length = n; }
  const box = el("wave");
  if (box.children.length !== n) box.innerHTML = "<span></span>".repeat(n);
  const max = session.mode === "practice" ? 34 : 42;
  [...box.children].forEach((b, i) => {
    const lv = session.levels[i] || 0;
    b.style.height = Math.max(4, Math.min(max, 4 + lv * max * 1.6)) + "px";
    b.className = i > n * 0.75 ? "old" : "";
  });
}

// ---------------------------------------------------------------- transcript
export function resetPanels() {
  const p = session.mode === "practice";
  $(UI[session.mode].transcript).innerHTML = `<p class="empty">${p ? "Your partner will start the conversation. Then just talk." : "Your words appear here as you speak."}</p>`;
  $(UI[session.mode].suggestions).innerHTML = `<p class="empty">Better phrasings, stronger words and what you could say next.</p>`;
  $(UI[session.mode].mistakes).innerHTML = `<p class="empty">Grammar and word mistakes appear here, each with a short fix.</p>`;
  $(UI[session.mode].count).hidden = true;
  if (!p) $("#liveMeta").textContent = "";
  pushLevel(0, true);
}

function lineHtml(u) {
  const t = fmtClock(u.at ?? 0);
  const marked = u.speaker === "me" ? highlight(u.text, (u.mistakes || []).map((m) => m.original), "err") : esc(u.text);
  const swap = u.speaker === "other" ? `Someone else · <button type="button" class="linkbtn" data-me="${u.id}">It was me</button>`
    : u.speaker === "me" ? `<button type="button" class="linkbtn" data-notme="${u.id}">Not me</button>` : "";
  if (session.mode === "practice") {
    const av = u.speaker === "agent" ? "AI" : u.speaker === "me" ? "You" : "?";
    const cls = u.speaker === "agent" ? "" : u.speaker === "me" ? "me" : "me other";
    return `<div class="bubble ${cls}" data-id="${u.id}"><span class="av">${av}</span>
      <div><p class="msg">${marked}${u.speaker === "agent" ? sayBtn(u.text) : ""}</p>${swap ? `<div class="who">${swap}</div>` : ""}</div></div>`;
  }
  return `<div class="line ${u.speaker}" data-id="${u.id}"><span class="t">${t}</span>
    <div class="body"><p>${marked}</p>${swap ? `<div class="who">${swap}</div>` : ""}</div></div>`;
}

function container() { const c = el("transcript"); c.querySelector(".empty")?.remove(); return c; }

function addLine(u) {
  removePending();
  u.at = now();
  const c = container();
  c.insertAdjacentHTML("beforeend", lineHtml(u));
  session.lines.set(u.id, u);
  scrollDown();
  updateMeta();
}

function rerender(u) {
  const node = el("transcript").querySelector(`[data-id="${u.id}"]`);
  if (node) node.outerHTML = lineHtml(u);
}

function showPending() {
  const c = container();
  if (c.querySelector(".pending")) return;
  c.insertAdjacentHTML("beforeend", session.mode === "practice"
    ? `<div class="bubble me pending"><span class="av">You</span><p class="msg">Writing down what you said<span class="caret"></span></p></div>`
    : `<div class="line pending"><span class="t">${fmtClock(now())}</span><div class="body"><p>Writing down what you said<span class="caret"></span></p></div></div>`);
  scrollDown();
}
function removePending() { el("transcript").querySelectorAll(".pending, .partial-row").forEach((x) => x.remove()); }
function scrollDown() { const t = el("transcript"); t.scrollTop = t.scrollHeight; }

function updateMeta() {
  if (session.mode !== "live") return;
  const mine = [...session.lines.values()].filter((u) => u.speaker === "me");
  const toReview = mine.reduce((n, u) => n + (u.mistakes?.length || 0), 0);
  $("#liveMeta").textContent = `${mine.length} sentence${mine.length === 1 ? "" : "s"}${toReview ? ` · ${toReview} to review` : ""}`;
}

document.addEventListener("click", async (e) => {
  const me = e.target.closest("[data-me]"), notme = e.target.closest("[data-notme]");
  if (!me && !notme) return;
  const id = +(me?.dataset.me || notme?.dataset.notme);
  try {
    const u = await api(`/api/utterances/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ speaker: me ? "me" : "other" }) });
    const old = session.lines.get(id) || {};
    const merged = { ...old, ...u, at: old.at };
    session.lines.set(id, merged);
    rerender(merged);
    el("mistakes").querySelectorAll(`[data-utt="${id}"]`).forEach((x) => x.remove());
    if (u.speaker === "me") addMistakes(id, u.mistakes);
    recount();
    updateMeta();
  } catch (err) { showError(err.message); }
});

// ---------------------------------------------------------------- panels
function applyAnalysis(m) {
  const u = session.lines.get(m.utterance_id);
  if (u) { Object.assign(u, { corrected: m.corrected, mistakes: m.mistakes, suggestions: m.suggestions }); rerender(u); }
  addMistakes(m.utterance_id, m.mistakes);
  showSuggestions(m.suggestions);
  updateMeta();
}

function addMistakes(uttId, list) {
  const box = el("mistakes");
  box.querySelector(".empty")?.remove();
  for (const x of [...(list || [])].reverse()) {
    box.insertAdjacentHTML("afterbegin", `<div class="mistake new" data-utt="${uttId}">
      <div class="fix">${fixHtml(x.original, x.corrected)}<span class="tag">${cap(x.category)}</span></div>
      <span class="why">${x.times_seen > 1 ? `<span class="tag rep" title="You have made this mistake ${x.times_seen} times">${x.times_seen}× so far</span> ` : ""}${esc(x.explanation)}</span></div>`);
  }
  recount();
}

function recount() {
  const n = el("mistakes").querySelectorAll(".mistake").length, badge = el("count");
  badge.hidden = !n;
  badge.textContent = n;
  if (!n && !el("mistakes").querySelector(".empty")) el("mistakes").innerHTML = `<p class="empty">No mistakes so far. Nicely done.</p>`;
}

const arrowRow = (text) => `<div class="next">${ICON.arrow("#4F46E5", 16)}<span>${esc(text)}</span>${sayBtn(text)}</div>`;

function showSuggestions(s) {
  if (!s) return;
  const parts = [];
  if (s.vocabulary?.length) parts.push(`<div class="group"><span class="label">Stronger words</span>${s.vocabulary.map((v) =>
    `<div class="word-chips"><span class="word-chip"><s>${esc(v.word)}</s></span>${v.better.map((b) => `<span class="word-chip">${esc(b)}</span>`).join("")}</div>`).join("")}</div>`);
  if (s.better_phrasing) parts.push(`<div class="group"><span class="label">Say it better</span><p class="quote"><span>“${esc(s.better_phrasing)}”</span>${sayBtn(s.better_phrasing)}</p></div>`);
  if (s.next_sentences?.length) parts.push(`<div class="group"><span class="label">What you could say next</span>${s.next_sentences.map(arrowRow).join("")}</div>`);
  if (!parts.length) parts.push(`<p class="sub">That sounded natural. Keep going.</p>`);
  el("suggestions").innerHTML = `<div id="${session.mode}Hints"></div>` + parts.join("");
}

// ---------------------------------------------------------------- while you speak
function onPartial(text) {
  const c = el("transcript");
  c.querySelector(".partial-row")?.remove();
  if (text && !c.querySelector(".pending")) {
    c.querySelector(".empty")?.remove();
    c.insertAdjacentHTML("beforeend", session.mode === "practice"
      ? `<div class="bubble me partial-b partial-row"><span class="av">You</span><p class="msg">${esc(text)}<span class="caret"></span></p></div>`
      : `<div class="line partial-row"><span class="t">${fmtClock(now())}</span><div class="body"><p><span class="partial">${esc(text)}</span><span class="caret"></span></p></div></div>`);
    scrollDown();
  }
  const h = session.hint;
  h.text = text;
  clearTimeout(h.timer);
  if (!text || text.split(" ").length < 3 || session.mic?.muted) return;
  const fire = () => {
    if (!h.text || h.text === h.sent || session.ws?.readyState !== 1) return;
    h.sent = h.text; h.last = Date.now();
    session.ws.send(JSON.stringify({ type: "partial", text: h.text }));
  };
  if (Date.now() - h.last > HINT_MAX_GAP_MS) fire(); else h.timer = setTimeout(fire, HINT_PAUSE_MS);
}

function showHints(m) {
  if (!session.hint.text) return; // the sentence has already finished
  let box = $(`#${session.mode}Hints`);
  if (!box) { el("suggestions").innerHTML = `<div id="${session.mode}Hints"></div>`; box = $(`#${session.mode}Hints`); }
  if (!m.fixes.length && !m.continuations.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="group live"><span class="label">While you speak</span>
    ${m.fixes.map((f) => `<div class="fix">${fixHtml(f.wrong, f.right)}</div>`).join("")}
    ${m.continuations.map((c) => `<div class="next">${ICON.arrow("#4F46E5", 16)}<span>${esc(c)}</span></div>`).join("")}</div>`;
}
