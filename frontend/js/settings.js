// Settings: record the user's voice once, so other people's sentences are shown but not graded.
import { encodeWav } from "./mic.js";
import { $, api, esc, postJSON, showError } from "./util.js";

const RECORD_SECONDS = 20, CLIP_SECONDS = 5;

export async function openSettings() {
  await refresh();
  $("#settings").showModal();
}

async function refresh() {
  const h = await api("/api/health");
  const v = h.voice_profile;
  $("#voiceStatus").innerHTML = v.enrolled
    ? `✓ Your voice is recorded. ${v.enabled ? "Only your sentences are checked." : "Voice check is <b>off</b>: every sentence is checked."}`
    : "No voice recorded yet: every sentence is treated as yours.";
  $("#voiceEnabled").checked = v.enabled;
  $("#voiceEnabled").disabled = !v.enrolled;
  $("#voiceDelete").hidden = !v.enrolled;
  $("#enrollBtn").textContent = v.enrolled ? "Record again" : "Record my voice";
}

$("#voiceEnabled").onchange = async (e) => { await postJSON("/api/voice/enabled", { enabled: e.target.checked }); refresh(); };
$("#voiceDelete").onclick = async () => { await api("/api/voice", { method: "DELETE" }); refresh(); };

// Record ~20 s of the user reading aloud, cut into 5 s clips, send for enrolment
$("#enrollBtn").onclick = async () => {
  const btn = $("#enrollBtn"), msg = $("#enrollMsg");
  $("#enrollBox").hidden = false;
  btn.disabled = true;
  let stream, ctx;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } });
    ctx = new AudioContext();
    await ctx.audioWorklet.addModule("/static/js/mic-worklet.js");
    const node = new AudioWorkletNode(ctx, "mic-tap"), src = ctx.createMediaStreamSource(stream);
    const chunks = [];
    node.port.onmessage = (e) => {
      chunks.push(e.data);
      let s = 0; for (const x of e.data) s += x * x;
      $("#enrollLevel").style.width = Math.min(100, Math.sqrt(s / e.data.length) * 600) + "%";
    };
    src.connect(node);
    for (let t = RECORD_SECONDS; t > 0; t--) { msg.textContent = `Recording… read the text above (${t} s)`; await new Promise((r) => setTimeout(r, 1000)); }
    src.disconnect(); node.disconnect();
    msg.textContent = "Checking your voice…";
    const all = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
    let o = 0; for (const c of chunks) { all.set(c, o); o += c.length; }
    const per = Math.floor(ctx.sampleRate * CLIP_SECONDS), fd = new FormData();
    for (let i = 0; i + per <= all.length; i += per) fd.append("clips", encodeWav(all.subarray(i, i + per), ctx.sampleRate), `clip${i}.wav`);
    const r = await api("/api/voice/enroll", { method: "POST", body: fd });
    msg.textContent = `Done. Your voice is saved (${r.clips} clips, match threshold ${r.threshold}).`;
  } catch (e) {
    msg.textContent = "";
    showError(e.message);
  } finally {
    stream?.getTracks().forEach((t) => t.stop());
    ctx?.close();
    btn.disabled = false;
    $("#enrollLevel").style.width = "0";
    refresh();
  }
};

export { esc };
