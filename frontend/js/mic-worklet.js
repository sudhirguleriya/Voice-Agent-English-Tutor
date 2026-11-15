// Audio worklet: hands raw mic samples (mono, AudioContext sample rate) to the main thread in ~85 ms chunks.
class MicTap extends AudioWorkletProcessor {
  constructor() { super(); this.buf = []; this.n = 0; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      this.buf.push(ch.slice());
      this.n += ch.length;
      if (this.n >= 4096) {
        const out = new Float32Array(this.n);
        let o = 0;
        for (const b of this.buf) { out.set(b, o); o += b.length; }
        this.port.postMessage(out);
        this.buf = []; this.n = 0;
      }
    }
    return true;
  }
}
registerProcessor("mic-tap", MicTap);
