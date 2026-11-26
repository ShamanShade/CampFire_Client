/* Audio for voice chat in the browser, running on the audio thread.
   cf-capture: cuts the microphone into 20 ms frames (960 samples at 48 kHz), like the desktop app.
   cf-player:  a small jitter buffer per person, mixed into one output. */

const FRAME = 960, PREFILL = 2, MAX_QUEUE = 10;

class Capture extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Float32Array(FRAME); this.n = 0; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.buf[this.n++] = ch[i];
      if (this.n === FRAME) {
        this.port.postMessage(this.buf, [this.buf.buffer]);
        this.buf = new Float32Array(FRAME);
        this.n = 0;
      }
    }
    return true;
  }
}

class Player extends AudioWorkletProcessor {
  constructor() {
    super();
    this.people = new Map();           // name -> {frames, off, started}
    this.port.onmessage = (e) => {
      const { user, pcm, drop, clear } = e.data;
      if (clear) { this.people.clear(); return; }
      if (drop) { this.people.delete(user); return; }
      let p = this.people.get(user);
      if (!p) { p = { frames: [], off: 0, started: false }; this.people.set(user, p); }
      p.frames.push(pcm);
      if (p.frames.length > MAX_QUEUE) { p.frames.splice(0, p.frames.length - MAX_QUEUE); p.off = 0; }
    };
  }
  process(_inputs, outputs) {
    const out = outputs[0][0];
    out.fill(0);
    for (const p of this.people.values()) {
      if (!p.started) { if (p.frames.length >= PREFILL) p.started = true; else continue; }
      let i = 0;
      while (i < out.length && p.frames.length) {
        const f = p.frames[0], take = Math.min(out.length - i, f.length - p.off);
        for (let k = 0; k < take; k++) out[i + k] += f[p.off + k];
        i += take; p.off += take;
        if (p.off >= f.length) { p.frames.shift(); p.off = 0; }
      }
      if (!p.frames.length) p.started = false;          // ran dry: wait for a little buffer again
    }
    for (let i = 0; i < out.length; i++) out[i] = out[i] > 1 ? 1 : out[i] < -1 ? -1 : out[i];
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    return true;
  }
}

registerProcessor("cf-capture", Capture);
registerProcessor("cf-player", Player);
