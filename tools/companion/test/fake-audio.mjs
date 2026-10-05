// A minimal Web Audio fake: enough surface for the music and voice engines,
// recording what was scheduled so tests can assert on it.
class Param {
  constructor(v = 0) { this.value = v; this.events = []; }
  setValueAtTime(v, t) { this.events.push(["set", v, t]); this.value = v; }
  linearRampToValueAtTime(v, t) { this.events.push(["ramp", v, t]); }
  setTargetAtTime(v, t, c) { this.events.push(["target", v, t, c]); this.value = v; }
}
class Node {
  constructor(ctx, kind) { this.ctx = ctx; this.kind = kind; this.outs = []; ctx.created.push(this); }
  connect(n) { this.outs.push(n); return n; }
  disconnect() { this.outs = []; }
}
export class FakeAudioContext {
  constructor() {
    this.created = []; this.currentTime = 0; this.sampleRate = 8000; this.state = "suspended";
    this.destination = new Node(this, "destination");
    FakeAudioContext.last = this;
  }
  createGain() { const n = new Node(this, "gain"); n.gain = new Param(1); return n; }
  createDynamicsCompressor() { return new Node(this, "compressor"); }
  createBiquadFilter() { const n = new Node(this, "filter"); n.frequency = new Param(350); n.Q = new Param(1); return n; }
  createConvolver() { return new Node(this, "convolver"); }
  createAnalyser() {
    const n = new Node(this, "analyser"); n.fftSize = 256; n.level = 0;
    n.getByteTimeDomainData = (arr) => arr.fill(128 + Math.round(n.level * 127));
    return n;
  }
  createOscillator() {
    const n = new Node(this, "osc"); n.frequency = new Param(440); n.detune = new Param(0);
    n.start = (t) => { n.startAt = t; }; n.stop = (t) => { n.stopAt = t; }; return n;
  }
  createBufferSource() {
    const n = new Node(this, "source");
    n.start = (t = 0) => { n.startAt = t; }; n.stop = () => { n.stopped = true; };
    n.onended = null; return n;
  }
  createBuffer(ch, len, rate) {
    const data = Array.from({ length: ch }, () => new Float32Array(len));
    return { numberOfChannels: ch, length: len, sampleRate: rate, duration: len / rate, getChannelData: (i) => data[i] };
  }
  async decodeAudioData(buf) { return { duration: 1.5, byteLength: buf.byteLength }; }
  async resume() { this.state = "running"; }
  async suspend() { this.state = "suspended"; }
  of(kind) { return this.created.filter((n) => n.kind === kind); }
}
