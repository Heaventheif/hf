export class DubbingSocket {
  constructor(url, handlers = {}) { this.url = url; this.handlers = handlers; this.ws = null; this.pending = new Map(); }
  connect(hello) {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.url); this.ws.binaryType = "arraybuffer";
      this.ws.onopen = () => { this.ws.send(JSON.stringify(hello)); resolve(); };
      this.ws.onerror = () => reject(new Error("تعذر الاتصال بخادم الدبلجة"));
      this.ws.onclose = () => this.handlers.onClose?.();
      this.ws.onmessage = (event) => this.onMessage(event.data);
    });
  }
  onMessage(data) {
    if (typeof data === "string") {
      const msg = JSON.parse(data);
      if (msg.type === "segment") this.pending.set(msg.seq, msg);
      this.handlers.onJson?.(msg);
      return;
    }
    const view = new DataView(data); const seq = view.getUint32(4); const meta = this.pending.get(seq);
    this.pending.delete(seq);
    this.handlers.onAudio?.(meta, new Int16Array(data.slice(8)));
  }
  sendPcm(session, offset, buffer) {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    const out = new ArrayBuffer(8 + buffer.byteLength); const view = new DataView(out);
    view.setUint32(0, session); view.setUint32(4, offset); new Uint8Array(out, 8).set(new Uint8Array(buffer)); this.ws.send(out);
  }
  send(message) { if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(message)); }
  close() { this.ws?.close(); this.ws = null; this.pending.clear(); }
}
