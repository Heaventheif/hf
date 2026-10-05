export class AudioScheduler {
  constructor() { this.ctx = new AudioContext(); this.sources = new Set(); this.videoTime = 0; this.lag = 4; this.gain = this.ctx.createGain(); this.gain.gain.value = 1; this.gain.connect(this.ctx.destination); }
  setVideoTime(time) { this.videoTime = Number(time) || 0; }
  reset() { for (const source of this.sources) { try { source.stop(); } catch {} } this.sources.clear(); }
  play(meta, pcm, sampleRate) {
    if (!meta || !pcm?.length) return;
    const buffer = this.ctx.createBuffer(1, pcm.length, sampleRate); buffer.copyToChannel(Float32Array.from(pcm, (v) => v / 32768), 0);
    const source = this.ctx.createBufferSource(); source.buffer = buffer; source.connect(this.gain);
    const due = Number(meta.src_start) + this.lag - this.videoTime;
    const when = this.ctx.currentTime + Math.max(0, due);
    source.start(when); this.sources.add(source); source.onended = () => this.sources.delete(source);
  }
  async pause() { await this.ctx.suspend(); }
  async resume() { await this.ctx.resume(); }
  close() { this.reset(); this.ctx.close(); }
}
