// Microphone: always-on capture with voice-activity detection (VAD) that adapts to room noise.
// Each sentence (cut at a pause) is emitted as a 16 kHz mono WAV. Silence is never sent.
// Also runs the browser's own speech recognition for an instant, partial transcript while you talk.

const TARGET_RATE = 16000;
const MAX_SEGMENT_MS = 25000;
const MIN_VOICED_MS = 450;
const PREROLL_CHUNKS = 8;

export class Mic {
  constructor({ onSegment, onLevel, onSpeaking, silenceMs = 1100 }) {
    Object.assign(this, { onSegment, onLevel, onSpeaking, silenceMs });
    this.on = false;
    this.muted = false; // true while the AI partner (or a 🔊 clip) is talking, so it is not recorded
    this.reset();
    this.noise = 0.004;
  }

  reset() { Object.assign(this, { speaking: false, chunks: [], preroll: [], voicedMs: 0, quietMs: 0, totalMs: 0, above: 0 }); }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    this.ctx = new AudioContext();
    await this.ctx.audioWorklet.addModule("/static/js/mic-worklet.js");
    this.src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "mic-tap");
    this.node.port.onmessage = (e) => this.chunk(e.data);
    this.src.connect(this.node);
    this.on = true;
    try { this.wake = await navigator.wakeLock?.request("screen"); } catch {}
  }

  stop() {
    if (this.speaking) this.endSegment(); // flush the sentence in progress
    this.on = false;
    this.node?.disconnect(); this.src?.disconnect();
    this.stream?.getTracks().forEach((t) => t.stop());
    this.ctx?.close();
    try { this.wake?.release(); } catch {}
    this.reset();
    this.onLevel?.(0);
  }

  chunk(buf) {
    const ms = (buf.length / this.ctx.sampleRate) * 1000;
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    const rms = Math.sqrt(sum / buf.length);
    const threshold = Math.max(0.012, this.noise * 3);
    this.onLevel?.(Math.min(1, rms * 6));
    if (this.muted) { if (this.speaking) this.reset(); return; }

    if (!this.speaking) {
      this.noise = this.noise * 0.97 + Math.min(rms, 0.05) * 0.03;
      this.preroll.push(buf);
      if (this.preroll.length > PREROLL_CHUNKS) this.preroll.shift();
      this.above = rms > threshold ? this.above + 1 : 0;
      if (this.above >= 3) {
        this.speaking = true;
        this.chunks = this.preroll.slice();
        this.preroll = [];
        this.voicedMs = ms * this.above; this.quietMs = 0; this.totalMs = ms * this.chunks.length;
        this.onSpeaking?.(true);
      }
      return;
    }
    this.chunks.push(buf);
    this.totalMs += ms;
    if (rms > threshold * 0.8) { this.voicedMs += ms; this.quietMs = 0; } else this.quietMs += ms;
    if (this.quietMs >= this.silenceMs || this.totalMs >= MAX_SEGMENT_MS) this.endSegment();
  }

  endSegment() {
    const { chunks, voicedMs } = this;
    const rate = this.ctx?.sampleRate || 48000;
    const speechMs = Math.max(0, this.totalMs - this.quietMs);
    this.reset();
    this.onSpeaking?.(false);
    if (voicedMs < MIN_VOICED_MS) return;
    this.onSegment?.(encodeWav(downsample(concat(chunks), rate, TARGET_RATE), TARGET_RATE), Math.round(speechMs));
  }
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
    const a = Math.floor(i * ratio), b = Math.min(input.length, Math.floor((i + 1) * ratio));
    let s = 0;
    for (let j = a; j < b; j++) s += input[j];
    out[i] = s / Math.max(1, b - a);
  }
  return out;
}

export function encodeWav(samples, rate) {
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

// Browser speech recognition (Chrome/Edge): instant partial words. Display + hints only, never graded,
// because it silently fixes grammar.
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
export class Partials {
  constructor(onText) { this.onText = onText; this.rec = null; this.offset = 0; this.results = 0; this.on = false; this.blocked = !SR; }

  start() {
    if (this.blocked || this.rec) return;
    this.on = true;
    const r = new SR();
    r.lang = "en-US"; r.continuous = true; r.interimResults = true;
    r.onresult = (e) => {
      this.results = e.results.length;
      let t = "";
      for (let i = this.offset; i < e.results.length; i++) t += e.results[i][0].transcript + " ";
      this.onText(t.replace(/\s+/g, " ").trim());
    };
    r.onerror = (e) => { if (e.error === "not-allowed" || e.error === "service-not-allowed") this.blocked = true; };
    r.onend = () => { if (this.rec === r) this.rec = null; this.offset = 0; this.results = 0; if (this.on && !this.blocked) setTimeout(() => this.start(), 200); };
    try { r.start(); this.rec = r; } catch {}
  }

  sentenceDone() { this.offset = this.results; this.onText(""); } // the VAD cut a sentence: start fresh
  stop() { this.on = false; const r = this.rec; this.rec = null; try { r?.abort(); } catch {} this.onText(""); }
}
