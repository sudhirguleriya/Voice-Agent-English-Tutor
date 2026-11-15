// Audio output: play the AI partner's replies and 🔊 clips; AI voice first, browser voice as the last resort.
// While anything plays, the mic is muted so the coach never records its own voice.
import { esc, ICON } from "./util.js";

let mic = null;                 // set by the session so playback can mute it
let current = null;
const clipCache = new Map();    // text -> object URL

export const attachMic = (m) => (mic = m);

async function withMute(fn) {
  const was = mic?.muted;
  if (mic) mic.muted = true;
  try { await fn(); } finally {
    await new Promise((r) => setTimeout(r, 300)); // let the room echo die down
    if (mic) mic.muted = !!was;
  }
}

export function stopAudio() {
  try { speechSynthesis.cancel(); } catch {}
  if (current) { current.pause(); current = null; }
}

function playUrl(url, rate = 1) {
  return new Promise((res, rej) => {
    stopAudio();
    const a = new Audio(url);
    current = a;
    a.preservesPitch = true;
    a.playbackRate = rate;
    a.onended = () => { if (current === a) current = null; res(); };
    a.onpause = () => res();
    a.onerror = () => rej(new Error("Couldn't play the audio."));
    a.play().catch(rej);
  });
}

function browserVoice(text, rate = 0.95) {
  return new Promise((res) => {
    if (!("speechSynthesis" in window)) return res();
    stopAudio();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-US"; u.rate = rate;
    u.onend = u.onerror = res;
    speechSynthesis.speak(u);
  });
}

// The partner's reply: audio from the server (base64), or the browser voice if the server had none
export function playReply(text, b64, mime) {
  return withMute(async () => {
    if (b64) {
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const url = URL.createObjectURL(new Blob([bytes], { type: mime || "audio/wav" }));
      try { await playUrl(url); return; } catch { /* fall through */ } finally { setTimeout(() => URL.revokeObjectURL(url), 60000); }
    }
    await browserVoice(text);
  });
}

// 🔊 any sentence: cached AI voice (server), else browser voice
export function say(text, rate = 1) {
  return withMute(async () => {
    try {
      let url = clipCache.get(text);
      if (!url) {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), 8000);
        const r = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }), signal: ctrl.signal }).finally(() => clearTimeout(t));
        if (!r.ok) throw new Error("tts");
        url = URL.createObjectURL(await r.blob());
        clipCache.set(text, url);
      }
      await playUrl(url, rate);
    } catch { await browserVoice(text, 0.95 * rate); }
  });
}

export const sayBtn = (text) => `<button type="button" class="say" data-say="${esc(text)}" aria-label="Listen" title="Listen">${ICON.speaker}</button>`;

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-say]");
  if (!b) return;
  b.classList.add("busy");
  say(b.dataset.say).finally(() => b.classList.remove("busy"));
});
