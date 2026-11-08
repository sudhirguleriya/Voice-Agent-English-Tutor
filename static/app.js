// Voice Coach front end: always-on mic + voice-activity detection + feedback UI + daily report.
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const today = () => new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD in local time

// ---------------------------------------------------------------- settings
const store = {
  get(k, d) { try { const v = localStorage.getItem("vc." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("vc." + k, JSON.stringify(v)); } catch {} },
};
const cfg = { speak: store.get("speak", false), silenceMs: store.get("silenceMs", 1100) };
$("#speakToggle").checked = cfg.speak;
$("#silence").value = cfg.silenceMs;
$("#silenceVal").textContent = (cfg.silenceMs / 1000).toFixed(1) + "s";
$("#speakToggle").onchange = (e) => store.set("speak", (cfg.speak = e.target.checked));
$("#silence").oninput = (e) => {
  cfg.silenceMs = +e.target.value;
  $("#silenceVal").textContent = (cfg.silenceMs / 1000).toFixed(1) + "s";
  store.set("silenceMs", cfg.silenceMs);
};

// ---------------------------------------------------------------- tabs
document.querySelectorAll(".tab").forEach((b) => (b.onclick = () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === b.dataset.tab));
  if (b.dataset.tab === "report") loadReport();
}));

// ---------------------------------------------------------------- mic + VAD
const TARGET_RATE = 16000;
const MAX_UTTERANCE_MS = 25000;
const MIN_VOICED_MS = 450;
const PREROLL_CHUNKS = 8;

const mic = {
  on: false, ctx: null, stream: null, node: null, src: null,
  noise: 0.004, speaking: false, chunks: [], preroll: [], voicedMs: 0, silenceMs: 0, totalMs: 0, above: 0,
  muted: false, // true while we speak feedback aloud, so we don't coach ourselves
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
  $("#micBtn").setAttribute("aria-pressed", "true");
  setStatus("Listening… just talk naturally.");
  // keep running if the device would otherwise sleep
  try { mic.wake = await navigator.wakeLock?.request("screen"); } catch {}
  // stream can end (device unplugged) — reflect it
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
  $("#micBtn").setAttribute("aria-pressed", "false");
  $("#micBtn").classList.remove("hearing");
  $("#level").style.width = "0";
  setStatus("Paused. Press the mic to listen again.");
}

$("#micBtn").onclick = () => (mic.on ? stopMic() : startMic());

// Re-acquire the wake lock when the tab becomes visible again.
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
      $("#micBtn").classList.add("hearing");
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
  Object.assign(mic, { speaking: false, chunks: [], voicedMs: 0, silenceMs: 0, totalMs: 0, above: 0 });
  $("#micBtn").classList.remove("hearing");
  if (mic.on) setStatus("Listening…");
  if (voiced < MIN_VOICED_MS) return;
  enqueue(encodeWav(downsample(concat(chunks), rate, TARGET_RATE), TARGET_RATE));
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

// ---------------------------------------------------------------- analysis queue
const queue = [];
let busy = false;

function enqueue(wav) {
  const card = addPendingCard();
  queue.push({ wav, card });
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
  const { wav, card } = queue.shift();
  updateQueue();
  try {
    const fd = new FormData();
    fd.append("audio", wav, "speech.wav");
    fd.append("day", today());
    const r = await fetch("/api/analyze", { method: "POST", body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    if (data.skipped) card.remove();
    else {
      renderCard(card, data);
      dayItems.push(data);
      updateToday();
      if (cfg.speak) await speakFeedback(data);
    }
  } catch (e) {
    card.classList.remove("pending");
    card.innerHTML = `<span class="error">Could not analyse that sentence: ${esc(e.message)}</span>`;
  } finally {
    busy = false;
    updateQueue();
    pump();
  }
}

// ---------------------------------------------------------------- speech output
function say(text, rate = 0.95) {
  return new Promise((res) => {
    if (!("speechSynthesis" in window)) return res();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-US"; u.rate = rate;
    u.onend = u.onerror = res;
    speechSynthesis.speak(u);
  });
}

async function speakFeedback(d) {
  const parts = [];
  if (d.mistakes.length) parts.push("Better to say: " + (d.better_version || d.corrected));
  d.pronunciation.slice(0, 2).forEach((p) => parts.push(`Pronounce: ${p.word}.`));
  if (!parts.length) return;
  mic.muted = true; // don't record our own voice
  setStatus("Speaking feedback…");
  for (const p of parts) await say(p);
  await new Promise((r) => setTimeout(r, 300));
  mic.muted = false;
  if (mic.on) setStatus("Listening…");
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-say]");
  if (!b) return;
  const wasMuted = mic.muted;
  mic.muted = true;
  say(b.dataset.say, +(b.dataset.rate || 0.85)).then(() => setTimeout(() => (mic.muted = wasMuted), 300));
});
const sayBtn = (text, label = "🔊") => `<button class="say" data-say="${esc(text)}" title="Hear it">${label}</button>`;

// ---------------------------------------------------------------- live feed
let dayItems = [];

function addPendingCard() {
  $("#feed .empty")?.remove();
  const c = document.createElement("div");
  c.className = "card pending";
  c.textContent = "Transcribing and checking…";
  $("#feed").prepend(c);
  return c;
}

function highlight(original, correction) {
  return `<del>${esc(original)}</del> → <ins>${esc(correction)}</ins>`;
}

function renderCard(card, d) {
  card.classList.remove("pending");
  const cls = d.score >= 85 ? "ok" : d.score < 60 ? "low" : "";
  const mistakes = d.mistakes || [];
  const vocab = d.vocabulary_upgrades || [];
  const pron = d.pronunciation || [];
  card.innerHTML = `
    <span class="actions"><button data-del="${d.id}" title="Delete (e.g. not you speaking)">✕</button></span>
    <span class="score ${cls}">${d.score ?? "–"}</span>
    <span class="label">You said</span>
    <div class="said">${esc(d.text)}</div>
    ${mistakes.length ? `
      <div class="row"><span class="label">Corrected</span><div class="fix">${esc(d.corrected)}</div></div>` : ""}
    ${d.better_version && d.better_version.trim() !== d.text.trim() ? `
      <div class="row"><span class="label">Say it better</span><div class="better">${esc(d.better_version)} ${sayBtn(d.better_version)}</div></div>` : ""}
    ${mistakes.length ? `<div class="row"><span class="label">Mistakes</span><ul class="items">${mistakes.map((m) => `
      <li><span class="chip">${esc(m.category.replace(/_/g, " "))}</span>${m.times_seen > 1 ? `<span class="chip rep">repeated ×${m.times_seen}</span>` : ""}
        ${highlight(m.original, m.correction)} — ${esc(m.explanation)}</li>`).join("")}</ul></div>` : `<div class="row fix">✓ No grammar mistakes.</div>`}
    ${vocab.length ? `<div class="row"><span class="label">Better vocabulary</span><ul class="items">${vocab.map((v) => `
      <li>Instead of <b>${esc(v.word)}</b>: ${(v.alternatives || []).map(esc).join(", ")}${v.example ? ` — <i>${esc(v.example)}</i>` : ""}</li>`).join("")}</ul></div>` : ""}
    ${pron.length ? `<div class="row"><span class="label">Pronunciation</span><ul class="items">${pron.map((p) => `
      <li><b>${esc(p.word)}</b> ${sayBtn(p.word)}${p.heard_as ? ` <span class="muted">(heard: ${esc(p.heard_as)})</span>` : ""} — ${esc(p.tip)}</li>`).join("")}</ul></div>` : ""}
    ${d.tip ? `<div class="tip">💡 ${esc(d.tip)}</div>` : ""}`;
}

$("#feed").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-del]");
  if (!b) return;
  await fetch("/api/utterance/" + b.dataset.del, { method: "DELETE" });
  dayItems = dayItems.filter((x) => String(x.id) !== b.dataset.del);
  b.closest(".card").remove();
  updateToday();
});

function updateToday() {
  const words = dayItems.reduce((n, d) => n + (d.word_count ?? d.text.split(/\s+/).length), 0);
  const mis = dayItems.reduce((n, d) => n + (d.mistakes?.length || 0), 0);
  const scores = dayItems.map((d) => d.score).filter((s) => s != null);
  $("#tUtt").textContent = dayItems.length;
  $("#tMis").textContent = mis;
  $("#tRate").textContent = words ? ((mis * 100) / words).toFixed(1) : "–";
  $("#tScore").textContent = scores.length ? Math.round(scores.reduce((a, b) => a + b) / scores.length) : "–";
}

async function loadToday() {
  const items = await (await fetch("/api/day/" + today())).json();
  dayItems = items.map((u) => ({ ...u, better_version: u.better }));
  if (dayItems.length) $("#feed .empty")?.remove();
  for (const d of dayItems) {
    const c = document.createElement("div");
    c.className = "card";
    renderCard(c, d);
    $("#feed").prepend(c);
  }
  updateToday();
}

// ---------------------------------------------------------------- daily report
$("#reportDay").value = today();
$("#reportDay").onchange = loadReport;
$("#refreshReport").onclick = loadReport;
$("#planBtn").onclick = async () => {
  const b = $("#planBtn");
  b.disabled = true; b.textContent = "Writing plan…";
  try {
    const r = await fetch("/api/report/plan?day=" + $("#reportDay").value, { method: "POST" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail);
    await loadReport();
  } catch (e) { alert(e.message); }
  b.disabled = false; b.textContent = "Generate practice plan";
};

function delta(t, y, lowerIsBetter) {
  if (t == null || y == null) return "";
  const d = +(t - y).toFixed(1);
  if (!d) return `<span class="delta">= same</span>`;
  const good = lowerIsBetter ? d < 0 : d > 0;
  return `<span class="delta ${good ? "good" : "bad"}">${d > 0 ? "▲" : "▼"} ${Math.abs(d)}</span>`;
}

function tile(label, t, y, lowerIsBetter) {
  return `<div class="tile"><span class="label">${label}</span><div class="v">${t ?? "–"} ${delta(t, y, lowerIsBetter)}</div>
    <div class="y">yesterday: ${y ?? "–"}</div></div>`;
}

function patTable(rows, cols) {
  if (!rows.length) return `<p class="muted">None.</p>`;
  return `<div class="scroll"><table><tr><th>Mistake pattern</th><th>Example</th>${cols.map((c) => `<th>${c[0]}</th>`).join("")}</tr>
    ${rows.map((p) => `<tr><td><span class="chip">${esc((p.category || "").replace(/_/g, " "))}</span> ${esc(p.pattern)}</td>
      <td>${p.example?.original ? highlight(p.example.original, p.example.correction) : ""}</td>
      ${cols.map((c) => `<td class="n">${c[1](p)}</td>`).join("")}</tr>`).join("")}</table></div>`;
}

async function loadReport() {
  const day = $("#reportDay").value || today();
  const body = $("#reportBody");
  body.innerHTML = `<p class="muted">Loading…</p>`;
  const r = await (await fetch("/api/report?day=" + day)).json();
  const T = r.totals.today, Y = r.totals.yesterday;
  const maxCat = Math.max(1, ...r.categories.flatMap((c) => [c.today, c.yesterday]));
  const maxTrend = Math.max(1, ...r.trend.map((x) => x.per100 || 0));
  const P = r.patterns;

  body.innerHTML = `
    ${r.plan ? `<div class="plan"><h2>${esc(r.plan.headline)}</h2><p>${esc(r.plan.progress)}</p>
      <h3>Focus on</h3><ul class="items">${r.plan.focus.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      <h3>Say these out loud</h3><ul class="items">${r.plan.drills.map((x) => `<li>${esc(x)} ${sayBtn(x)}</li>`).join("")}</ul>
      <h3>Pronunciation practice</h3><ul class="items">${r.plan.pronounce.map((x) => `<li>${esc(x)} ${sayBtn(x.split(/[—:(-]/)[0])}</li>`).join("")}</ul>
      <h3>New words to use</h3><ul class="items">${r.plan.new_words.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}

    <h2>${esc(r.day)} compared with ${esc(r.yesterday)}</h2>
    <div class="tiles">
      ${tile("Mistakes / 100 words", T.per100, Y.per100, true)}
      ${tile("Total mistakes", T.mistakes, Y.mistakes, true)}
      ${tile("Average score", T.avg_score, Y.avg_score, false)}
      ${tile("Sentences", T.utterances, Y.utterances, false)}
      ${tile("Words spoken", T.words, Y.words, false)}
    </div>

    ${r.trend.length > 1 ? `<h2>Mistakes per 100 words — last ${r.trend.length} days</h2>
      <div class="trend">${r.trend.map((x) => `<div class="${x.day === r.day ? "cur" : ""}" title="${x.day}: ${x.per100} per 100 words">
        <span>${x.per100 ?? ""}</span><span class="b" style="height:${((x.per100 || 0) / maxTrend) * 70}%"></span><span>${x.day.slice(5)}</span></div>`).join("")}</div>` : ""}

    <h2>Mistakes by type</h2>
    ${r.categories.length ? `<table><tr><th>Type</th><th>Today <span class="muted">vs</span> yesterday</th><th class="n">Today</th><th class="n">Yesterday</th></tr>
      ${r.categories.map((c) => `<tr><td>${esc(c.category.replace(/_/g, " "))}</td>
        <td><div class="bar"><i style="width:${(c.today / maxCat) * 120}px"></i></div><div class="bar"><i class="y" style="width:${(c.yesterday / maxCat) * 120}px"></i></div></td>
        <td class="n">${c.today}</td><td class="n">${c.yesterday} ${delta(c.today, c.yesterday, true)}</td></tr>`).join("")}</table>` : `<p class="muted">No mistakes recorded.</p>`}

    <h2>🔁 Your most repeated mistakes (all time)</h2>
    ${patTable(P.all_time_top, [["Today", (p) => p.today], ["Total", (p) => p.total], ["Days seen", (p) => p.days]])}

    <h2>Repeated more than once today</h2>
    ${patTable(P.repeated_today, [["Today", (p) => p.today], ["Yesterday", (p) => p.yesterday]])}

    <h2>⚠️ Still making since yesterday</h2>
    ${patTable(P.still_making, [["Today", (p) => p.today], ["Yesterday", (p) => p.yesterday], ["Days seen", (p) => p.days]])}

    <h2>✅ Fixed — made yesterday, not today</h2>
    ${patTable(P.fixed_since_yesterday, [["Yesterday", (p) => p.yesterday]])}

    <h2>New today</h2>
    ${patTable(P.new_today, [["Today", (p) => p.today]])}

    <h2>Filler words</h2>
    ${r.fillers.length ? `<table><tr><th>Word</th><th class="n">Today</th><th class="n">Yesterday</th></tr>
      ${r.fillers.map((f) => `<tr><td>“${esc(f.word)}”</td><td class="n">${f.today}</td><td class="n">${f.yesterday} ${delta(f.today, f.yesterday, true)}</td></tr>`).join("")}</table>` : `<p class="muted">No filler words. 👏</p>`}

    ${r.overused_words.length ? `<h3>Words you used a lot today</h3><p>${r.overused_words.map((w) => `<span class="chip">${esc(w.word)} ×${w.count}</span>`).join(" ")}</p>` : ""}

    <h2>Vocabulary upgrades</h2>
    ${r.vocabulary.length ? `<table><tr><th>You said</th><th>Try instead</th><th class="n">Today</th><th class="n">Yesterday</th></tr>
      ${r.vocabulary.map((v) => `<tr><td>${esc(v.word)}</td><td>${v.alternatives.map(esc).join(", ")}</td><td class="n">${v.n}</td><td class="n">${v.yesterday}</td></tr>`).join("")}</table>` : `<p class="muted">None yet.</p>`}

    <h2>Pronunciation</h2>
    ${r.pronunciation.length ? `<table><tr><th>Word</th><th>Tip</th><th class="n">Today</th><th class="n">Yesterday</th><th class="n">Days</th></tr>
      ${r.pronunciation.map((p) => `<tr><td><b>${esc(p.word)}</b> ${sayBtn(p.word)}</td><td>${esc(p.tip)}</td><td class="n">${p.n}</td><td class="n">${p.yesterday}</td><td class="n">${p.all_days}</td></tr>`).join("")}</table>` : `<p class="muted">No pronunciation problems detected.</p>`}
  `;
}

// ---------------------------------------------------------------- boot
fetch("/api/health").then((r) => r.json()).then((h) => { $("#keyWarn").hidden = h.key_configured; });
loadToday();
