class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.inputBuffer = [];
    this.outputBuffer = [];
    this.inputRate = sampleRate;
    this.sourcePos = 0;
    this.offset = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel?.length) return true;
    for (let i = 0; i < channel.length; i++) this.inputBuffer.push(channel[i]);

    const step = this.inputRate / 16000;
    while (this.sourcePos + 1 < this.inputBuffer.length) {
      const i = Math.floor(this.sourcePos);
      const frac = this.sourcePos - i;
      this.outputBuffer.push(this.inputBuffer[i] * (1 - frac) + this.inputBuffer[i + 1] * frac);
      this.sourcePos += step;
    }
    const consumed = Math.max(0, Math.floor(this.sourcePos));
    if (consumed) {
      this.inputBuffer.splice(0, consumed);
      this.sourcePos -= consumed;
    }

    while (this.outputBuffer.length >= 8000) {
      const pcm = new Int16Array(8000);
      for (let i = 0; i < pcm.length; i++) {
        pcm[i] = Math.max(-32768, Math.min(32767, this.outputBuffer[i] * 32767));
      }
      this.outputBuffer.splice(0, 8000);
      this.port.postMessage({ pcm: pcm.buffer, offset: this.offset }, [pcm.buffer]);
      this.offset += pcm.length;
    }
    return true;
  }
}
registerProcessor("capture-processor", CaptureProcessor);
