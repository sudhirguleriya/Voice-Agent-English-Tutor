// Voice Coach front end: always-on mic + voice-activity detection, live feedback, spaced-repetition practice, progress.
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const today = () => new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD in local time
const nice = (s) => esc(String(s || "").replace(/_/g, " "));

// ---------------------------------------------------------------- settings
const store = {
  get(k, d) { try { const v = localStorage.getItem("vc." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("vc." + k, JSON.stringify(v)); } catch {} },
};
const cfg = { speak: store.get("speak", false), silenceMs: store.get("silenceMs", 1100), live: store.get("live", true) };
$("#speakToggle").checked = cfg.speak;
$("#liveToggle").checked = cfg.live;
$("#liveToggle").onchange = (e) => {
  store.set("live", (cfg.live = e.target.checked));
  if (cfg.live && mic.on) startLive(); else stopLive();
  syncLivePanel();
};
$("#silence").value = cfg.silenceMs;
$("#silenceVal").textContent = (cfg.silenceMs / 1000).toFixed(1) + "s";
$("#speakToggle").onchange = (e) => store.set("speak", (cfg.speak = e.target.checked));
$("#silence").oninput = (e) => {
  cfg.silenceMs = +e.target.value;
  $("#silenceVal").textContent = (cfg.silenceMs / 1000).toFixed(1) + "s";
  store.set("silenceMs", cfg.silenceMs);
};

// ---------------------------------------------------------------- errors + API
function showError(msg) {
  const b = $("#banner");
  b.textContent = msg + "  (click to dismiss)";
  b.hidden = false;
  clearTimeout(showError.t);
  showError.t = setTimeout(() => (b.hidden = true), 15000);
}
$("#banner").onclick = () => ($("#banner").hidden = true);

// fetch JSON; always gives a readable error even when the server sends plain text
async function api(url, opts) {
  let r;
  try { r = await fetch(url, opts); } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new Error("Can't reach the Voice Coach server. Is it still running?");
  }
  const txt = await r.text();
  let d;
  try { d = txt ? JSON.parse(txt) : {}; } catch { d = { detail: txt.slice(0, 200) || r.statusText }; }
  if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : r.statusText);
  return d;
}

// ---------------------------------------------------------------- tabs
let activeTab = "live";
function showTab(name) {
  activeTab = name;
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === name));
  if (mic.target?.kind === "practice" && name !== "practice") clearTarget();
  if (name === "report") loadReport();
  if (name === "practice") loadPractice();
  if (name === "convs") loadConvs();
}
document.querySelectorAll(".tab").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

// ---------------------------------------------------------------- mic + VAD
const TARGET_RATE = 16000;
const MAX_UTTERANCE_MS = 25000;
const MIN_VOICED_MS = 450;
const PREROLL_CHUNKS = 8;

const mic = {
  on: false, ctx: null, stream: null, node: null, src: null,
  noise: 0.004, speaking: false, chunks: [], preroll: [], voicedMs: 0, silenceMs: 0, totalMs: 0, above: 0,
  muted: false,  // true while we speak aloud, so we don't coach ourselves
  target: null,  // when set, the next sentence is checked against a target instead of analysed
};

async function startMic() {
  try {
    mic.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
  } catch (e) {
    setStatus("Microphone permission denied: " + e.message, true);
    return;
  }
  mic.ctx = new AudioContext();
  await mic.ctx.audioWorklet.addModule("/static/mic-worklet.js");
  mic.src = mic.ctx.createMediaStreamSource(mic.stream);
  mic.node = new AudioWorkletNode(mic.ctx, "mic-tap");
  mic.node.port.onmessage = (e) => onChunk(e.data);
  mic.src.connect(mic.node);
  mic.on = true;
  conv.fresh = true; // turning the mic on starts a new conversation
  syncMicUi();
  startLive();
  syncLivePanel();
  setStatus(mic.target ? "Listening for your answer…" : "Listening… just talk naturally.");
  try { mic.wake = await navigator.wakeLock?.request("screen"); } catch {}
  mic.stream.getTracks()[0].onended = () => { stopMic(); setStatus("Microphone disconnected.", true); };
}

function stopMic() {
  if (mic.speaking) endUtterance();
  mic.on = false;
  mic.node?.disconnect(); mic.src?.disconnect();
  mic.stream?.getTracks().forEach((t) => t.stop());
  mic.ctx?.close();
  try { mic.wake?.release(); } catch {}
  Object.assign(mic, { ctx: null, stream: null, node: null, src: null, speaking: false, chunks: [], preroll: [] });
  syncMicUi();
  stopLive();
  syncLivePanel();
  $("#level").style.width = "0";
  setStatus("Paused. Press the mic to listen again.");
}

function syncMicUi() {
  $("#micBtn").setAttribute("aria-pressed", String(mic.on));
  $("#micPill").setAttribute("aria-pressed", String(mic.on));
  $("#micPillText").textContent = mic.on ? (mic.target ? "Answer mode" : "Listening") : "Mic off";
  if (!mic.on) { $("#micBtn").classList.remove("hearing"); $("#micPill").classList.remove("hearing"); }
}

const toggleMic = () => (mic.on ? stopMic() : startMic());
$("#micBtn").onclick = toggleMic;
$("#micPill").onclick = toggleMic;

document.addEventListener("visibilitychange", async () => {
  if (mic.on && document.visibilityState === "visible") { try { mic.wake = await navigator.wakeLock?.request("screen"); } catch {} }
});

function onChunk(buf) {
  const ms = (buf.length / mic.ctx.sampleRate) * 1000;
  let sum = 0;
  for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
  const rms = Math.sqrt(sum / buf.length);
  const threshold = Math.max(0.012, mic.noise * 3);

  $("#level").style.width = Math.min(100, rms * 600) + "%";
  $("#thresh").style.left = Math.min(100, threshold * 600) + "%";

  if (mic.muted) return;

  if (!mic.speaking) {
    mic.noise = mic.noise * 0.97 + Math.min(rms, 0.05) * 0.03; // adapt to room noise
    mic.preroll.push(buf);
    if (mic.preroll.length > PREROLL_CHUNKS) mic.preroll.shift();
    mic.above = rms > threshold ? mic.above + 1 : 0;
    if (mic.above >= 3) {
      mic.speaking = true;
      mic.chunks = mic.preroll.slice();
      mic.preroll = [];
      mic.voicedMs = ms * mic.above; mic.silenceMs = 0; mic.totalMs = ms * mic.chunks.length;
      $("#micBtn").classList.add("hearing"); $("#micPill").classList.add("hearing");
      setStatus("Hearing you…");
    }
    return;
  }

  mic.chunks.push(buf);
  mic.totalMs += ms;
  if (rms > threshold * 0.8) { mic.voicedMs += ms; mic.silenceMs = 0; } else mic.silenceMs += ms;

  if (mic.silenceMs >= cfg.silenceMs || mic.totalMs >= MAX_UTTERANCE_MS) endUtterance();
}

function endUtterance() {
  const chunks = mic.chunks, voiced = mic.voicedMs, rate = mic.ctx?.sampleRate || 48000;
  const speechMs = Math.max(0, mic.totalMs - mic.silenceMs); // speaking time, without the closing pause
  Object.assign(mic, { speaking: false, chunks: [], voicedMs: 0, silenceMs: 0, totalMs: 0, above: 0 });
  $("#micBtn").classList.remove("hearing"); $("#micPill").classList.remove("hearing");
  if (mic.on) setStatus(mic.target ? "Listening for your answer…" : "Listening…");
  if (voiced >= MIN_VOICED_MS) liveSentenceDone();
  if (voiced < MIN_VOICED_MS) return;
  const wav = encodeWav(downsample(concat(chunks), rate, TARGET_RATE), TARGET_RATE);
  if (mic.target) submitAttempt(wav, mic.target);
  else enqueue(wav, Math.round(speechMs));
}

function concat(chunks) {
  const out = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out;
}

function downsample(input, from, to) {
  if (from === to) return input;
  const ratio = from / to, out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const start = Math.floor(i * ratio), end = Math.min(input.length, Math.floor((i + 1) * ratio));
    let s = 0;
    for (let j = start; j < end; j++) s += input[j];
    out[i] = s / Math.max(1, end - start);
  }
  return out;
}

function encodeWav(samples, rate) {
  const buf = new ArrayBuffer(44 + samples.length * 2), v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  w(36, "data"); v.setUint32(40, samples.length * 2, true);
  for (let i = 0, o = 44; i < samples.length; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

function setStatus(t, err) { const s = $("#status"); s.textContent = t; s.classList.toggle("error", !!err); }

// ---------------------------------------------------------------- answer mode (say a target sentence)
function setTarget(t) { mic.target = t; syncMicUi(); if (mic.on) setStatus("Listening for your answer…"); }
function clearTarget() { mic.target = null; syncMicUi(); if (mic.on) setStatus("Listening…"); }

async function submitAttempt(wav, t) {
  if (t.once) clearTarget();
  t.onPending?.();
  const fd = new FormData();
  fd.append("audio", wav, "attempt.wav");
  fd.append("target", t.text);
  fd.append("must", t.must || "");
  fd.append("pattern", t.pattern || "");
  fd.append("grade", t.grade?.() ? "1" : "0");
  fd.append("day", today());
  try {
    const d = await api("/api/attempt", { method: "POST", body: fd });
    if (d.skipped && t.once) setTarget(t); // nothing heard: stay in answer mode
    t.onResult(d);
  } catch (e) {
    if (t.once) setTarget(t);
    t.onError ? t.onError(e) : showError(e.message);
  }
}

// shows what you said against the target: green = missing words, red strike = said instead
function attemptDiff(r) {
  return (r.diff || []).map((x) => x.op === "eq" ? esc(x.w)
    : x.op === "miss" ? `<ins class="miss" title="you didn't say this">${esc(x.w)}</ins>`
    : `<del title="you said this instead">${esc(x.w)}</del>`).join(" ");
}

function attemptHtml(r) {
  if (r.skipped) return `<span class="muted">Didn't catch that. Say it again.</span>`;
  const pct = Math.round((r.similarity || 0) * 100);
  return `<div class="verdict ${r.passed ? "pass" : "fail"}">${r.passed ? "✓ Well done, that matches" : "✗ Not quite"}
      <span class="muted">${pct}% match</span></div>
    <div class="heard"><span class="label">What you said</span>${attemptDiff(r)}</div>
    ${r.passed ? "" : `<div class="muted">Green = words you missed · red = what you said instead.${r.must_ok === false ? " The key correction wasn't there." : ""}</div>`}`;
}

// ---------------------------------------------------------------- analysis queue
const queue = [];
let busy = false;
const conv = { id: null, fresh: true }; // current conversation; the server assigns the id

function enqueue(wav, durationMs) {
  const card = addPendingCard();
  queue.push({ wav, card, durationMs });
  updateQueue();
  pump();
}

function updateQueue() {
  const n = queue.length + (busy ? 1 : 0);
  $("#queue").textContent = n ? `Analysing ${n} sentence${n > 1 ? "s" : ""}…` : "";
}

async function pump() {
  if (busy || !queue.length) return;
  busy = true;
  const { wav, card, durationMs } = queue.shift();
  updateQueue();
  try {
    const fd = new FormData();
    fd.append("audio", wav, "speech.wav");
    fd.append("day", today());
    fd.append("duration_ms", String(durationMs || 0));
    if (conv.id) fd.append("conversation_id", String(conv.id));
    fd.append("new_conversation", conv.fresh ? "1" : "0");
    const data = await api("/api/analyze", { method: "POST", body: fd });
    if (data.skipped) card.remove();
    else {
      if (data.conversation_id) { conv.id = data.conversation_id; conv.fresh = false; }
      renderCard(card, data);
      renderNext(data.next_suggestions);
      dayItems.push(data);
      updateToday();
      if (cfg.speak) await speakFeedback(data);
    }
  } catch (e) {
    card.classList.remove("pending");
    card.innerHTML = `<span class="error">Couldn't analyse that sentence: ${esc(e.message)}</span>`;
  } finally {
    busy = false;
    updateQueue();
    pump();
  }
}

// ---------------------------------------------------------------- speech output
// AI voice (server: gpt-audio-mini -> Fish Audio S2 Pro), cached per sentence; browser voice if that fails or is turned off.
cfg.voice = store.get("voice", "ai");
$("#voiceSel").value = cfg.voice;
$("#voiceSel").onchange = (e) => store.set("voice", (cfg.voice = e.target.value));
let serverVoice = true;          // set from /api/health
const clipCache = new Map();     // text -> object URL
let nowPlaying = null, voiceWarned = false;

function stopSpeech() {
  try { speechSynthesis.cancel(); } catch {}
  if (nowPlaying) { nowPlaying.pause(); nowPlaying = null; }
}

async function say(text, rate = 0.95) {
  stopSpeech();
  if (cfg.voice === "ai" && serverVoice) {
    try { return await playAi(text, rate); }
    catch (e) {
      if (!voiceWarned) { voiceWarned = true; showError("AI voice unavailable, using the browser voice. " + e.message); }
    }
  }
  return sayBrowser(text, rate);
}

async function fetchClip(text) {
  if (clipCache.has(text)) return clipCache.get(text);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000); // then the browser voice speaks; the server still caches the clip
  try {
    const r = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }), signal: ctrl.signal });
    if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
    const url = URL.createObjectURL(await r.blob());
    clipCache.set(text, url);
    return url;
  } catch (e) {
    throw e.name === "AbortError" ? new Error("The AI voice took too long.") : e;
  } finally { clearTimeout(timer); }
}

async function playAi(text, rate) {
  const url = await fetchClip(text);
  return new Promise((res, rej) => {
    const a = new Audio(url);
    nowPlaying = a;
    a.preservesPitch = true;
    a.playbackRate = Math.max(0.6, Math.min(1.2, rate / 0.95)); // 0.95 = normal, lower = slower practice
    a.onended = () => { if (nowPlaying === a) nowPlaying = null; res(); };
    a.onpause = () => res(); // stopped by a newer clip
    a.onerror = () => rej(new Error("Couldn't play the audio."));
    a.play().catch(rej);
  });
}

function sayBrowser(text, rate) {
  return new Promise((res) => {
    if (!("speechSynthesis" in window)) return res();
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-US"; u.rate = rate;
    u.onend = u.onerror = res;
    speechSynthesis.speak(u);
  });
}

async function sayMuted(text, rate) {
  const was = mic.muted;
  mic.muted = true;
  await say(text, rate);
  await new Promise((r) => setTimeout(r, 300));
  mic.muted = was;
}

async function speakFeedback(d) {
  const parts = [];
  if (d.mistakes.length) parts.push("Better to say: " + (d.better_version || d.corrected));
  d.pronunciation.slice(0, 2).forEach((p) => parts.push(`Pronounce: ${p.word}.`));
  if (!parts.length) return;
  setStatus("Speaking feedback…");
  for (const p of parts) await sayMuted(p);
  if (mic.on) setStatus("Listening…");
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-say]");
  if (!b) return;
  b.classList.add("busy");
  sayMuted(b.dataset.say, +(b.dataset.rate || 0.85)).finally(() => b.classList.remove("busy"));
});
const sayBtn = (text, label = "🔊", rate) =>
  `<button class="say" data-say="${esc(text)}"${rate ? ` data-rate="${rate}"` : ""} title="Hear it">${label}</button>`;

// ---------------------------------------------------------------- live feed
let dayItems = [];
const byId = new Map();

function addPendingCard() {
  $("#feed .empty")?.remove();
  const c = document.createElement("div");
  c.className = "card pending";
  c.innerHTML = `<span class="spinner"></span> Transcribing and checking…`;
  $("#feed").prepend(c);
  return c;
}

// word-level diff: shows `b` with words removed from `a` struck out and new words highlighted
function wordDiff(a, b) {
  const A = String(a || "").split(/\s+/).filter(Boolean), B = String(b || "").split(/\s+/).filter(Boolean);
  const k = (w) => w.toLowerCase().replace(/[^a-z0-9']/g, "");
  const n = A.length, m = B.length;
  const L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    L[i][j] = k(A[i]) === k(B[j]) ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (k(A[i]) === k(B[j])) { out.push(esc(B[j])); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) out.push(`<del>${esc(A[i++])}</del>`);
    else out.push(`<ins>${esc(B[j++])}</ins>`);
  }
  while (i < n) out.push(`<del>${esc(A[i++])}</del>`);
  while (j < m) out.push(`<ins>${esc(B[j++])}</ins>`);
  return out.join(" ");
}

const wpmOf = (d) => (d.duration_ms > 500 ? Math.round(((d.word_count ?? d.text.split(/\s+/).length) * 60000) / d.duration_ms) : null);
const timeOf = (d) => (d.ts ? d.ts.slice(11, 16) : "");

function renderCard(card, d) {
  card.classList.remove("pending");
  card.dataset.id = d.id;
  byId.set(String(d.id), d);
  const cls = d.score >= 85 ? "ok" : d.score < 60 ? "low" : "";
  const mistakes = d.mistakes || [];
  const vocab = d.vocabulary_upgrades || [];
  const pron = d.pronunciation || [];
  const grammar = mistakes.filter((m) => m.category !== "filler");
  const fillers = mistakes.filter((m) => m.category === "filler");
  const better = (d.better_version || "").trim();
  const target = better || d.corrected || d.text;
  const wpm = wpmOf(d);
  card.innerHTML = `
    <div class="card-top">
      <span class="score ${cls}" title="Overall quality of this sentence">${d.score ?? "–"}</span>
      <span class="muted">${timeOf(d)}${wpm ? ` · ${wpm} wpm` : ""}</span>
      <span class="spacer"></span>
      <button class="icon" data-del="${d.id}" title="Delete (e.g. it wasn't you speaking)">✕</button>
    </div>
    <div class="three">
      <div class="v-said"><span class="label">You said</span><div class="said">${esc(d.text)}</div></div>
      <div class="v-fix"><span class="label">Corrected</span>
        <div class="said">${mistakes.length && d.corrected ? wordDiff(d.text, d.corrected) : esc(d.corrected || d.text)}</div>
        ${!grammar.length ? `<div class="ok-line">✓ Grammar is correct${fillers.length ? ", but watch the filler words" : ""}</div>` : ""}</div>
      <div class="v-better"><span class="label">Better way to say it</span>
        <div class="better">${esc(better || d.corrected || d.text)}</div>
        <div class="better-actions">${sayBtn(target, "🔊 Listen")}<button class="primary small" data-practice="${d.id}">🎙 Say it</button></div>
        <div class="attempt"></div></div>
    </div>
    ${grammar.length ? `<ul class="mistakes">${grammar.map((m) => `
      <li><div><span class="chip">${nice(m.category)}</span>${m.times_seen > 1 ? `<span class="chip rep" title="You've made this mistake ${m.times_seen} times">repeated ×${m.times_seen}</span>` : ""}
        <del>${esc(m.original)}</del> → <ins>${esc(m.correction)}</ins></div>
        <div class="why">${esc(m.explanation)}</div></li>`).join("")}</ul>` : ""}
    ${fillers.length ? `<div class="mini"><span class="label">Fillers</span> ${fillers.map((m) => `<span class="chip warn">${esc(m.original)}</span>`).join("")}
      <span class="muted">Pause silently instead.</span></div>` : ""}
    ${vocab.length ? `<div class="mini"><span class="label">Stronger words</span><ul class="items">${vocab.map((v) => `
      <li><b>${esc(v.word)}</b> → ${(v.alternatives || []).map(esc).join(", ")}${v.example ? `<div class="why"><i>${esc(v.example)}</i></div>` : ""}</li>`).join("")}</ul></div>` : ""}
    ${pron.length ? `<div class="mini"><span class="label">Pronunciation</span><ul class="items">${pron.map((p) => `
      <li><b>${esc(p.word)}</b> ${sayBtn(p.word, "🔊", 0.7)}${p.heard_as ? ` <span class="muted">sounded like “${esc(p.heard_as)}”</span>` : ""}<div class="why">${esc(p.tip)}</div></li>`).join("")}</ul></div>` : ""}
    ${d.tip ? `<div class="tip">💡 ${esc(d.tip)}</div>` : ""}`;
  card.dataset.target = target;
}

$("#feed").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    try { await api("/api/utterance/" + del.dataset.del, { method: "DELETE" }); } catch (err) { return showError(err.message); }
    dayItems = dayItems.filter((x) => String(x.id) !== del.dataset.del);
    del.closest(".card").remove();
    updateToday();
    return;
  }
  const pr = e.target.closest("[data-practice]");
  if (pr) { const card = pr.closest(".card"); armSayIt(card.querySelector(".attempt"), card.dataset.target); }
});

// "Say it": the next sentence you speak is checked against `text` (not analysed, not counted in stats)
async function armSayIt(box, text) {
  document.querySelectorAll(".attempt.armed").forEach((x) => { if (x !== box) { x.classList.remove("armed"); x.innerHTML = ""; } });
  if (!mic.on) await startMic();
  if (!mic.on) return;
  box.classList.add("armed");
  box.innerHTML = `<span class="hint">🎙 Say it now. <button class="link" data-cancel>cancel</button></span>`;
  box.querySelector("[data-cancel]").onclick = () => { clearTarget(); box.classList.remove("armed"); box.innerHTML = ""; };
  setTarget({
    kind: "sayit", once: true, text,
    onPending: () => (box.innerHTML = `<span class="spinner"></span> Checking…`),
    onResult: (r) => {
      box.classList.toggle("armed", !!r.skipped);
      box.innerHTML = attemptHtml(r) + (r.passed || r.skipped ? "" : `<button class="small" data-retry>🎙 Try again</button> ${sayBtn(text, "🔊 Listen again")}`);
      const retry = box.querySelector("[data-retry]");
      if (retry) retry.onclick = () => armSayIt(box, text);
    },
    onError: (e) => { box.classList.remove("armed"); box.innerHTML = `<span class="error">${esc(e.message)}</span>`; },
  });
}

// ---------------------------------------------------------------- live suggestions WHILE speaking
// The browser's own speech recognition streams your words instantly; on every short pause (and every
// couple of seconds while you keep talking) a fast model returns fixes so far + ways to finish the sentence.
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const live = { rec: null, offset: 0, results: 0, text: "", sent: "", timer: null, lastReq: 0, ctrl: null, blocked: false };
const LIVE_PAUSE_MS = 600, LIVE_MAX_GAP_MS = 2200;

function syncLivePanel() {
  const show = mic.on && cfg.live;
  $("#livePanel").hidden = !show;
  if (show && !SR) liveNote("Your browser has no live speech recognition. Use Chrome or Edge for live suggestions.");
}
function liveNote(msg) { $("#lpState").textContent = msg; }

function startLive() {
  if (!SR || !cfg.live || !mic.on || live.rec || live.blocked) return;
  const r = new SR();
  r.lang = "en-US"; r.continuous = true; r.interimResults = true; r.maxAlternatives = 1;
  r.onresult = (e) => {
    live.results = e.results.length;
    // ignore our own voice (feedback read aloud) and answers in practice mode
    if (mic.muted || mic.target) { live.offset = e.results.length; return; }
    let t = "", interim = "";
    for (let i = live.offset; i < e.results.length; i++) {
      if (e.results[i].isFinal) t += e.results[i][0].transcript + " ";
      else interim += e.results[i][0].transcript;
    }
    live.text = (t + interim).replace(/\s+/g, " ").trim();
    $("#lpText").innerHTML = live.text ? `${esc(t)}<span class="interim">${esc(interim)}</span><span class="caret"></span>` : "";
    $("#livePanel").classList.remove("stale");
    scheduleLive();
  };
  r.onerror = (e) => {
    if (e.error === "not-allowed" || e.error === "service-not-allowed") {
      live.blocked = true;
      liveNote("The browser blocked live speech recognition, so live suggestions are off. Normal coaching still works.");
    } else if (e.error === "network") liveNote("Live recognition needs internet.");
  };
  r.onend = () => { // Chrome stops recognition now and then; keep it running while the mic is on
    if (live.rec === r) live.rec = null;
    live.offset = 0; live.results = 0;
    if (mic.on && cfg.live && !live.blocked) setTimeout(startLive, 200);
  };
  try { r.start(); live.rec = r; liveNote(""); } catch { /* already started */ }
}

function stopLive() {
  const r = live.rec;
  live.rec = null;
  try { r?.abort(); } catch {}
  live.ctrl?.abort();
  clearTimeout(live.timer);
}

function scheduleLive() {
  clearTimeout(live.timer);
  if (live.text.split(" ").length < 2) return;
  if (Date.now() - live.lastReq > LIVE_MAX_GAP_MS) requestLive();         // talking continuously
  else live.timer = setTimeout(requestLive, LIVE_PAUSE_MS);               // short pause / hesitation
}

async function requestLive() {
  const text = live.text;
  if (!text || text === live.sent || mic.target) return;
  live.sent = text; live.lastReq = Date.now();
  live.ctrl?.abort();
  const ctrl = (live.ctrl = new AbortController());
  $("#lpState").innerHTML = `<span class="spinner"></span>`;
  try {
    const d = await api("/api/live", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: ctrl.signal,
      body: JSON.stringify({ text, context: dayItems.slice(-3).map((x) => x.text) }),
    });
    if (ctrl !== live.ctrl) return;
    liveNote(d.disabled ? "LIVE_MODEL is empty in .env, so live suggestions are off." : "");
    renderLive(d);
  } catch (e) {
    if (e.name !== "AbortError") liveNote(e.message);
  }
}

function renderLive(d) {
  $("#lpFix").innerHTML = d.fixes?.length
    ? `<span class="label">Fix as you go</span>${d.fixes.map((f) => `<span class="fix-chip"><del>${esc(f.wrong)}</del> → <ins>${esc(f.right)}</ins></span>`).join("")}`
    : (live.text ? `<span class="ok-line">✓ Correct so far</span>` : "");
  $("#lpCont").innerHTML = d.continuations?.length
    ? `<span class="label">You could finish with</span><ul>${d.continuations.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : "";
}

function liveSentenceDone() { // the VAD decided the sentence ended: start fresh for the next one
  live.offset = live.results;
  live.text = ""; live.sent = "";
  clearTimeout(live.timer);
  $("#livePanel").classList.add("stale");
}

// ---------------------------------------------------------------- "you could say next"
const TOPICS = [
  "What did you do this morning?", "Describe your job or studies.", "Tell me about a trip you enjoyed.",
  "What are your plans for this weekend?", "Describe your home town.", "What's a film or book you liked, and why?",
  "Explain how to cook your favourite dish.", "What did you do yesterday evening?", "Describe a friend or family member.",
  "What would you change about your daily routine?",
];

function renderNext(list) {
  const el = $("#nextBox");
  if (!list?.length) {
    const picks = [...TOPICS].sort(() => Math.random() - 0.5).slice(0, 3);
    el.innerHTML = `<div class="next-head">💬 <b>Not sure what to say?</b> <span class="muted">Pick a topic and talk for a minute.</span></div>
      <div class="topics">${picks.map((t) => `<span class="chip topic">${esc(t)}</span>`).join("")}</div>`;
    return;
  }
  el.innerHTML = `<div class="next-head">💬 <b>You could say next</b> <span class="muted">Correct sentences that continue your topic. Press 🎙 and say one.</span></div>
    <ul class="next-list">${list.map((s, i) => `<li><div class="next-row"><span class="next-text">${esc(s)}</span>
      <span class="next-actions">${sayBtn(s)}<button class="small" data-next="${i}">🎙 Say it</button></span></div>
      <div class="attempt"></div></li>`).join("")}</ul>`;
  el.querySelectorAll("[data-next]").forEach((b) => (b.onclick = () => armSayIt(b.closest("li").querySelector(".attempt"), list[+b.dataset.next])));
}

// ---------------------------------------------------------------- conversations (full text)
$("#convDay").value = today();
$("#convDay").onchange = loadConvs;
$("#refreshConvs").onclick = loadConvs;

const joinText = (arr) => arr.filter(Boolean).map((s) => s.trim()).join(" ");

async function loadConvs() {
  const body = $("#convBody");
  body.innerHTML = `<p class="muted">Loading…</p>`;
  let list;
  try { list = await api("/api/conversations?day=" + ($("#convDay").value || today())); }
  catch (e) { body.innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
  if (!list.length) { body.innerHTML = `<div class="empty">No conversations on this day yet. Go to <b>Live</b> and start talking.</div>`; return; }
  body.innerHTML = list.slice().reverse().map((c, i) => convHtml(c, list.length - i)).join("");
  body.querySelectorAll("[data-polish]").forEach((b) => (b.onclick = () => polishConv(b)));
  body.querySelectorAll("[data-view]").forEach((b) => (b.onclick = () => {
    const box = b.closest(".conv");
    box.querySelectorAll("[data-view]").forEach((x) => x.classList.toggle("active", x === b));
    box.querySelectorAll(".conv-view").forEach((v) => (v.hidden = v.dataset.v !== b.dataset.view));
  }));
}

function convHtml(c, n) {
  const S = c.sentences;
  const said = joinText(S.map((s) => s.text));
  const fixed = joinText(S.map((s) => s.corrected || s.text));
  const better = joinText(S.map((s) => s.better || s.corrected || s.text));
  const words = S.reduce((a, s) => a + (s.word_count || 0), 0);
  const mis = S.reduce((a, s) => a + s.mistakes, 0), fil = S.reduce((a, s) => a + s.fillers, 0);
  const P = c.polished;
  return `<div class="conv">
    <div class="conv-head"><div><h3>Conversation ${n}</h3>
      <span class="muted">${c.started.slice(11, 16)}–${c.ended.slice(11, 16)} · ${S.length} sentence${S.length > 1 ? "s" : ""} · ${words} words · ${mis} mistake${mis === 1 ? "" : "s"} · ${fil} filler${fil === 1 ? "" : "s"}</span></div>
      <div class="seg"><button class="small active" data-view="full">Full text</button><button class="small" data-view="lines">Sentence by sentence</button></div></div>

    <div class="conv-view" data-v="full">
      <div class="block said-b"><span class="label">What you said</span><p>${esc(said)}</p></div>
      <div class="block fix-b"><span class="label">Corrected (same sentences, mistakes fixed)</span><p>${S.map((s) => s.mistakes || s.fillers ? wordDiff(s.text, s.corrected || s.text) : esc(s.corrected || s.text)).join(" ")}</p></div>
      <div class="block better-b"><span class="label">Better way to say it</span><p>${esc(better)}</p><div>${sayBtn(better, "🔊 Listen")}</div></div>
    </div>

    <div class="conv-view" data-v="lines" hidden>
      <div class="scroll"><table><tr><th>#</th><th>You said</th><th>Corrected</th><th>Better way</th></tr>
      ${S.map((s, i) => `<tr><td class="n">${i + 1}</td><td>${esc(s.text)}</td>
        <td>${s.mistakes || s.fillers ? wordDiff(s.text, s.corrected || s.text) : `<span class="ok-line">✓</span> ${esc(s.corrected || s.text)}`}</td>
        <td>${esc(s.better || s.corrected || s.text)} ${sayBtn(s.better || s.corrected || s.text)}</td></tr>`).join("")}</table></div>
    </div>

    <div class="block polish-b">
      ${P ? `<div class="polish-head"><span class="label">✨ Whole conversation, retold naturally</span><button class="small" data-polish="${c.id}">Redo</button></div>
          <div class="polished">${esc(P.polished).split(/\n+/).map((p) => `<p>${p}</p>`).join("")}</div>
          <div>${sayBtn(P.polished, "🔊 Listen")}</div>
          ${P.key_changes?.length ? `<h4>Most useful changes</h4><ul class="items">${P.key_changes.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>` : ""}`
        : `<div class="polish-head"><div><b>✨ Polish the whole conversation</b><div class="muted">One smooth, natural version of everything you said: the best model to read aloud and copy.</div></div>
          <button class="primary small" data-polish="${c.id}">Polish</button></div>`}
      <div class="error" data-err></div>
    </div>
  </div>`;
}

async function polishConv(b) {
  b.disabled = true; b.textContent = "Writing…";
  try { await api(`/api/conversation/${b.dataset.polish}/polish`, { method: "POST" }); await loadConvs(); }
  catch (e) { b.disabled = false; b.textContent = "Try again"; b.closest(".polish-b").querySelector("[data-err]").textContent = e.message; }
}

function updateToday() {
  const words = dayItems.reduce((n, d) => n + (d.word_count ?? d.text.split(/\s+/).length), 0);
  const all = dayItems.flatMap((d) => d.mistakes || []);
  const scores = dayItems.map((d) => d.score).filter((s) => s != null);
  const timed = dayItems.filter((d) => d.duration_ms > 500);
  const tw = timed.reduce((n, d) => n + (d.word_count ?? d.text.split(/\s+/).length), 0);
  const tms = timed.reduce((n, d) => n + d.duration_ms, 0);
  $("#tUtt").textContent = dayItems.length;
  $("#tRate").textContent = words ? ((all.length * 100) / words).toFixed(1) : "–";
  $("#tScore").textContent = scores.length ? Math.round(scores.reduce((a, b) => a + b) / scores.length) : "–";
  $("#tWpm").textContent = tms ? Math.round((tw * 60000) / tms) : "–";
  $("#tFill").textContent = all.filter((m) => m.category === "filler").length;
}

async function loadToday() {
  try {
    const items = await api("/api/day/" + today());
    dayItems = items.map((u) => ({ ...u, better_version: u.better }));
    if (dayItems.length) $("#feed .empty")?.remove();
    renderNext(dayItems.at(-1)?.next_suggestions);
    for (const d of dayItems) {
      const c = document.createElement("div");
      c.className = "card";
      renderCard(c, d);
      $("#feed").prepend(c);
    }
    updateToday();
  } catch (e) { showError(e.message); }
}

function renderFocusLine(focus) {
  const el = $("#focusLine");
  if (!focus?.length) { el.hidden = true; return; }
  const f = focus[0];
  el.hidden = false;
  el.innerHTML = `🎯 <b>Watch out today:</b> ${esc(f.pattern)}
    ${f.example?.original ? `<span class="muted">(e.g. <del>${esc(f.example.original)}</del> → <ins>${esc(f.example.correction)}</ins>)</span>` : ""}
    <button class="link" data-goto="practice">practise it</button>`;
}
document.addEventListener("click", (e) => { const g = e.target.closest("[data-goto]"); if (g) showTab(g.dataset.goto); });

// ---------------------------------------------------------------- practice (spaced repetition)
const BOX_LABEL = ["new", "level 1", "level 2", "level 3", "level 4", "mastered"];
const P = { cards: [], i: 0, tries: 0, revealed: false, right: 0, done: 0, timer: null };

function updateDueBadge(n) { const b = $("#dueBadge"); b.hidden = !n; b.textContent = n; }

async function loadPractice() {
  const body = $("#practiceBody");
  body.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const d = await api("/api/practice?day=" + today());
    Object.assign(P, { cards: d.cards, i: 0, tries: 0, revealed: false, right: 0, done: 0 });
    const s = d.stats;
    $("#deckStats").innerHTML = `<div><b>${s.new}</b><span>new</span></div><div><b>${s.learning}</b><span>learning</span></div>
      <div><b>${s.mastered}</b><span>mastered</span></div>`;
    updateDueBadge(d.cards.length);
    renderPractice();
  } catch (e) { body.innerHTML = `<p class="error">${esc(e.message)}</p>`; }
}

function markFragment(sentence, frag) {
  const s = esc(sentence), f = esc(frag || "").trim();
  if (!f) return s;
  const i = s.toLowerCase().indexOf(f.toLowerCase());
  return i < 0 ? s : s.slice(0, i) + `<mark>${s.slice(i, i + f.length)}</mark>` + s.slice(i + f.length);
}

function renderPractice() {
  clearTimeout(P.timer);
  const body = $("#practiceBody");
  if (!P.cards.length) {
    if (mic.target?.kind === "practice") clearTarget();
    body.innerHTML = `<div class="pcard done"><h3>🎉 Nothing to practise right now</h3>
      <p class="muted">Talk on the <b>Live</b> tab. Each grammar mistake you make becomes a card here, and cards you've answered come back on their review day.</p></div>`;
    return;
  }
  if (P.i >= P.cards.length) {
    if (mic.target?.kind === "practice") clearTarget();
    body.innerHTML = `<div class="pcard done"><h3>Session complete: ${P.right} of ${P.done} right first time</h3>
      <p class="muted">Cards you got wrong come back tomorrow. Correct ones come back later and later until they're mastered.</p>
      <button class="primary" id="pAgain">Check for more cards</button></div>`;
    $("#pAgain").onclick = loadPractice;
    return;
  }
  const c = P.cards[P.i];
  body.innerHTML = `
    <div class="progress"><i style="width:${(P.i / P.cards.length) * 100}%"></i></div>
    <div class="pcard">
      <div class="pc-top"><span class="muted">Card ${P.i + 1} of ${P.cards.length}</span>
        <span class="chip">${nice(c.category)}</span><span class="chip ${c.box ? "" : "rep"}">${BOX_LABEL[c.box] || ""}</span>
        <span class="muted">made ${c.total}× on ${c.days} day${c.days > 1 ? "s" : ""}</span></div>
      <div class="rule">${esc(c.pattern)}</div>
      <span class="label">You said</span>
      <div class="wrong">${markFragment(c.wrong, c.original)}</div>
      <span class="label">Your turn</span>
      <p class="prompt">Say this sentence <b>correctly</b>, out loud.</p>
      <div id="pAnswerBox" class="answer" ${P.revealed ? "" : "hidden"}>
        <div>${esc(c.target)} ${sayBtn(c.target, "🔊", 0.85)}</div>
        <div class="why">${esc(c.explanation)}</div>
      </div>
      <div id="pResult" class="presult"></div>
      <div class="pc-actions">
        <button id="pMic" class="primary">${mic.on ? "🎙 Listening, speak now" : "🎙 Start mic & answer"}</button>
        <button id="pReveal" ${P.revealed ? "hidden" : ""}>Show answer</button>
        <button id="pSkip">Skip</button>
        <button id="pNext" hidden>Next →</button>
      </div>
    </div>`;
  $("#pMic").onclick = async () => { if (!mic.on) await startMic(); renderPracticeMic(); };
  $("#pReveal").onclick = () => reveal(true);
  $("#pSkip").onclick = () => nextCard();
  $("#pNext").onclick = () => nextCard();
  armPractice(c);
}

function renderPracticeMic() { const b = $("#pMic"); if (b) b.textContent = mic.on ? "🎙 Listening, speak now" : "🎙 Start mic & answer"; }

function reveal(speak) {
  P.revealed = true;
  $("#pAnswerBox").hidden = false;
  $("#pReveal").hidden = true;
  if (speak) sayMuted(P.cards[P.i].target, 0.85);
}

function nextCard() { P.i++; P.tries = 0; P.revealed = false; renderPractice(); }

function armPractice(c) {
  if (activeTab !== "practice") return;
  setTarget({
    kind: "practice", once: false, text: c.target, must: c.correction, pattern: c.pattern,
    grade: () => P.tries === 0 && !P.revealed, // only the first unaided try moves the card between boxes
    onPending: () => { const r = $("#pResult"); if (r) r.innerHTML = `<span class="spinner"></span> Checking…`; },
    onResult: (r) => {
      const box = $("#pResult");
      if (!box || P.cards[P.i] !== c) return;
      if (r.skipped) { box.innerHTML = attemptHtml(r); return; }
      const first = P.tries === 0 && !P.revealed;
      if (first) { P.done++; if (r.passed) P.right++; }
      P.tries++;
      let extra = "";
      if (r.schedule) extra = r.passed ? `<div class="muted">Next review in ${r.schedule.interval_days} day${r.schedule.interval_days > 1 ? "s" : ""}.</div>`
        : `<div class="muted">It will come back tomorrow. Try it once more now.</div>`;
      box.innerHTML = attemptHtml(r) + extra;
      if (r.passed) {
        clearTarget();
        if (!P.revealed) reveal(false);
        $("#pNext").hidden = false; $("#pSkip").hidden = true;
        P.timer = setTimeout(nextCard, 3500); // hands-free: move on automatically
      } else if (P.tries >= 2 && !P.revealed) reveal(true);
      else if (P.tries >= 4) { $("#pNext").hidden = false; }
    },
    onError: (e) => { const box = $("#pResult"); if (box) box.innerHTML = `<span class="error">${esc(e.message)}</span>`; },
  });
  renderPracticeMic();
}

// ---------------------------------------------------------------- progress report
$("#reportDay").value = today();
$("#reportDay").onchange = loadReport;
$("#refreshReport").onclick = loadReport;

function delta(t, y, mode) {
  if (t == null || y == null || mode === "neutral") return "";
  const d = +(t - y).toFixed(1);
  if (!d) return `<span class="delta">= same</span>`;
  const good = mode === "lower" ? d < 0 : d > 0;
  return `<span class="delta ${good ? "good" : "bad"}">${d > 0 ? "▲" : "▼"} ${Math.abs(d)}</span>`;
}

function tile(label, t, y, mode, hint) {
  return `<div class="tile"><span class="label">${label}</span><div class="v">${t ?? "–"} ${delta(t, y, mode)}</div>
    <div class="y">${hint ? hint + " · " : ""}yesterday: ${y ?? "–"}</div></div>`;
}

const STATUS = { new: ["new", "chip"], still: ["still making", "chip rep"], back: ["came back", "chip warn"] };

function patTable(rows, cols, withStatus) {
  if (!rows.length) return `<p class="muted">None.</p>`;
  return `<div class="scroll"><table><tr><th>Mistake</th><th>Example</th>${cols.map((c) => `<th class="n">${c[0]}</th>`).join("")}</tr>
    ${rows.map((p) => `<tr><td><div class="pat">${esc(p.pattern)}</div><span class="chip">${nice(p.category)}</span>${withStatus && STATUS[p.status] ? `<span class="${STATUS[p.status][1]}">${STATUS[p.status][0]}</span>` : ""}${p.box >= 4 ? `<span class="chip good">mastered</span>` : ""}</td>
      <td>${p.example?.original ? `<del>${esc(p.example.original)}</del> → <ins>${esc(p.example.correction)}</ins>` : ""}
        ${p.example?.explanation ? `<div class="why">${esc(p.example.explanation)}</div>` : ""}</td>
      ${cols.map((c) => `<td class="n">${c[1](p)}</td>`).join("")}</tr>`).join("")}</table></div>`;
}

function sparkline(trend, key, curDay) {
  const vals = trend.map((x) => x[key] ?? 0), max = Math.max(1, ...vals);
  return `<div class="trend">${trend.map((x) => `<div class="${x.day === curDay ? "cur" : ""}" title="${x.day}: ${x[key] ?? "–"}">
    <span>${x[key] ?? ""}</span><span class="b" style="height:${((x[key] || 0) / max) * 70}%"></span><span>${x.day.slice(5)}</span></div>`).join("")}</div>`;
}

function planHtml(plan) {
  if (!plan) return `<div class="plan empty-plan"><div><h3>Today's coaching plan</h3>
      <p class="muted">A short review of today, plus drill sentences built from your own mistakes.</p></div>
      <button id="planBtn" class="primary">Generate plan</button><div id="planErr" class="error"></div></div>`;
  return `<div class="plan"><div class="plan-head"><h3>${esc(plan.headline)}</h3><button id="planBtn" class="small">Regenerate</button></div>
    <p>${esc(plan.progress)}</p><div id="planErr" class="error"></div>
    <div class="plan-grid">
      <div><h4>Focus on</h4><ul class="items">${plan.focus.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
      <div><h4>Say these out loud</h4><ul class="items">${plan.drills.map((x) => `<li>${esc(x)} ${sayBtn(x)}</li>`).join("")}</ul></div>
      <div><h4>Pronunciation</h4><ul class="items">${plan.pronounce.map((x) => `<li>${esc(x)} ${sayBtn(x.split(/[—:(-]/)[0], "🔊", 0.7)}</li>`).join("")}</ul></div>
      <div><h4>New words to use</h4><ul class="items">${plan.new_words.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
    </div></div>`;
}

async function makePlan() {
  const b = $("#planBtn");
  b.disabled = true; b.textContent = "Writing plan…";
  try {
    await api("/api/report/plan?day=" + $("#reportDay").value, { method: "POST" });
    await loadReport();
  } catch (e) {
    b.disabled = false; b.textContent = "Try again";
    $("#planErr").textContent = e.message;
  }
}

async function loadReport() {
  const day = $("#reportDay").value || today();
  const body = $("#reportBody");
  body.innerHTML = `<p class="muted">Loading…</p>`;
  let r;
  try { r = await api("/api/report?day=" + day); } catch (e) { body.innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
  if (day === today()) renderFocusLine(r.focus);
  updateDueBadge(r.practice_due);
  const T = r.totals.today, Y = r.totals.yesterday, P_ = r.patterns, M = r.mastery;
  const maxCat = Math.max(1, ...r.categories.flatMap((c) => [c.today, c.yesterday]));

  body.innerHTML = `
    <h2 class="first">Your top weak spots</h2>
    ${r.focus.length ? `<div class="focus-grid">${r.focus.map((f, i) => `<div class="focus-card">
        <div class="rank">${i + 1}</div>
        <div class="pat">${esc(f.pattern)}</div>
        ${f.example?.original ? `<div><del>${esc(f.example.original)}</del> → <ins>${esc(f.example.correction)}</ins></div>` : ""}
        ${f.example?.explanation ? `<div class="why">${esc(f.example.explanation)}</div>` : ""}
        <div class="muted">${f.total}× on ${f.days} day${f.days > 1 ? "s" : ""}${f.today ? ` · ${f.today} today` : ""} · ${BOX_LABEL[f.box]}</div>
      </div>`).join("")}</div>
      <p><button class="primary" data-goto="practice">Practise now${r.practice_due ? ` (${r.practice_due} due)` : ""}</button>
        <span class="muted">  ${M.mastered} of ${M.patterns} mistake types mastered</span></p>`
      : `<p class="muted">No recurring mistakes yet. Keep talking.</p>`}

    <h2>${esc(r.day)} compared with ${esc(r.yesterday)}</h2>
    <div class="tiles">
      ${tile("Mistakes / 100 words", T.per100, Y.per100, "lower")}
      ${tile("Average score", T.avg_score, Y.avg_score, "higher")}
      ${tile("Clean sentences %", T.clean_pct, Y.clean_pct, "higher", "score 85+")}
      ${tile("Fillers / 100 words", T.fillers_per100, Y.fillers_per100, "lower", "aim < 2")}
      ${tile("Speaking speed (wpm)", T.wpm, Y.wpm, "neutral", "natural 120–160")}
      ${tile("Words spoken", T.words, Y.words, "higher")}
    </div>

    ${planHtml(r.plan)}

    ${r.trend.length > 1 ? `<div class="two-col">
      <div><h3>Mistakes per 100 words</h3>${sparkline(r.trend, "per100", r.day)}</div>
      <div><h3>Fillers per 100 words</h3>${sparkline(r.trend, "fillers_per100", r.day)}</div></div>` : ""}

    <h2>Today's mistakes <span class="muted">(fillers are listed separately below)</span></h2>
    ${(() => {
      const rows = P_.today_all.filter((p) => p.category !== "filler");
      const cols = [["Today", (p) => p.today], ["All time", (p) => p.total], ["Days", (p) => p.days]];
      return patTable(rows.slice(0, 8), cols, true) + (rows.length > 8
        ? `<details class="more"><summary>Show ${rows.length - 8} more</summary>${patTable(rows.slice(8), cols, true)}</details>` : "");
    })()}

    ${P_.fixed_since_yesterday.length ? `<h2>✅ Fixed: made yesterday, not today</h2>
      ${patTable(P_.fixed_since_yesterday, [["Yesterday", (p) => p.yesterday]])}` : ""}

    <h2>🔁 Most repeated of all time</h2>
    ${patTable(P_.all_time_top, [["Today", (p) => p.today], ["Total", (p) => p.total], ["Days", (p) => p.days]])}

    <h2>Mistakes by type</h2>
    ${r.categories.length ? `<table><tr><th>Type</th><th><span class="key t"></span>today <span class="key y"></span>yesterday</th><th class="n">Today</th><th class="n">Yesterday</th></tr>
      ${r.categories.map((c) => `<tr><td>${nice(c.category)}</td>
        <td><div class="bar"><i style="width:${(c.today / maxCat) * 100}%"></i></div><div class="bar"><i class="y" style="width:${(c.yesterday / maxCat) * 100}%"></i></div></td>
        <td class="n">${c.today}</td><td class="n">${c.yesterday} ${delta(c.today, c.yesterday, "lower")}</td></tr>`).join("")}</table>` : `<p class="muted">No mistakes recorded.</p>`}

    <div class="three-col">
      <div><h3>Filler words</h3>
        ${r.fillers.length ? `<table><tr><th>Word</th><th class="n">Today</th><th class="n">Yest.</th></tr>
          ${r.fillers.map((f) => `<tr><td>“${esc(f.word)}”</td><td class="n">${f.today}</td><td class="n">${f.yesterday} ${delta(f.today, f.yesterday, "lower")}</td></tr>`).join("")}</table>
          <p class="muted">Tip: when you need time to think, stay silent for a moment instead.</p>` : `<p class="muted">No filler words. 👏</p>`}
        ${r.overused_words.length ? `<h4>Words you used a lot</h4><p>${r.overused_words.map((w) => `<span class="chip">${esc(w.word)} ×${w.count}</span>`).join(" ")}</p>` : ""}
      </div>
      <div><h3>Vocabulary upgrades</h3>
        ${r.vocabulary.length ? `<table><tr><th>You said</th><th>Try instead</th></tr>
          ${r.vocabulary.map((v) => `<tr><td>${esc(v.word)} <span class="muted">×${v.n}</span></td><td>${v.alternatives.map(esc).join(", ")}</td></tr>`).join("")}</table>` : `<p class="muted">None yet.</p>`}
      </div>
      <div><h3>Pronunciation</h3>
        ${r.pronunciation.length ? `<table><tr><th>Word</th><th>How to say it</th></tr>
          ${r.pronunciation.map((p) => `<tr><td><b>${esc(p.word)}</b> ${sayBtn(p.word, "🔊", 0.7)}<div class="muted">${p.n}× today · ${p.all_days} day${p.all_days > 1 ? "s" : ""}</div></td><td>${esc(p.tip)}</td></tr>`).join("")}</table>` : `<p class="muted">No pronunciation problems detected.</p>`}
      </div>
    </div>
  `;
  $("#planBtn").onclick = makePlan;
}

// ---------------------------------------------------------------- boot
api("/api/health").then((h) => {
  $("#keyWarn").hidden = h.key_configured;
  serverVoice = !!h.ai_voice;
  if (!serverVoice) $("#voiceSel").value = "browser";
}).catch((e) => showError(e.message));
loadToday();
api("/api/report?day=" + today()).then((r) => { renderFocusLine(r.focus); updateDueBadge(r.practice_due); }).catch(() => {});
{ // deep links: /?tab=practice, /?tab=report&day=2026-11-07
  const q = new URLSearchParams(location.search);
  if (/^\d{4}-\d{2}-\d{2}$/.test(q.get("day") || "")) $("#reportDay").value = q.get("day");
  if (/^\d{4}-\d{2}-\d{2}$/.test(q.get("day") || "")) $("#convDay").value = q.get("day");
  if (["practice", "report", "convs"].includes(q.get("tab"))) showTab(q.get("tab"));
}
