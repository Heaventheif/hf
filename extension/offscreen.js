import { DubbingSocket } from "./ws-client.js";
import { AudioScheduler } from "./scheduler.js";
import { SyncManager } from "./sync.js";

let socket = null;
let scheduler = null;
let sync = null;
let captureContext = null;
let captureStream = null;
let worklet = null;

function wsUrl(spaceUrl) {
  const url = new URL(spaceUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws";
  return url.toString();
}

function status(state, detail = "") {
  chrome.runtime.sendMessage({ type: "OFFSCREEN_STATUS", state, detail }).catch(() => {});
}

async function stop() {
  socket?.close(); socket = null;
  worklet?.disconnect(); worklet = null;
  captureStream?.getTracks().forEach((track) => track.stop()); captureStream = null;
  captureContext?.close(); captureContext = null;
  scheduler?.close(); scheduler = null; sync = null;
  status("stopped");
}

async function start({ streamId, spaceUrl, mediaT0 }) {
  await stop();
  sync = new SyncManager(); sync.start(mediaT0);
  scheduler = new AudioScheduler();
  socket = new DubbingSocket(wsUrl(spaceUrl), {
    onJson: (message) => { if (message.type === "ready") status("connected", `RTT ready; sample rate ${message.tts_sample_rate}`); if (message.type === "error") status("error", message.message); },
    onAudio: (meta, pcm) => { if (!sync.ad) scheduler.play(meta, pcm, meta?.sample_rate || 22050); },
    onClose: () => status("disconnected")
  });
  await socket.connect({ type: "hello", session: sync.session, src_lang: "auto", voice: "ar_JO-kareem-medium", media_t0: mediaT0 || 0 });
  captureContext = new AudioContext();
  await captureContext.audioWorklet.addModule(chrome.runtime.getURL("capture-worklet.js"));
  captureStream = await navigator.mediaDevices.getUserMedia({ audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } }, video: false });
  const source = captureContext.createMediaStreamSource(captureStream);
  worklet = new AudioWorkletNode(captureContext, "capture-processor");
  worklet.port.onmessage = (event) => { if (!sync.ad) socket?.sendPcm(sync.session, event.data.offset, event.data.pcm); };
  source.connect(worklet); // intentionally not connected to destination: prevents original-audio echo
  status("running");
}

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "OFFSCREEN_START") start(message).catch((error) => status("error", error.message));
  else if (message.type === "OFFSCREEN_STOP") stop();
  else if (message.type === "TAB_CLOSED") stop();
  else if (message.type === "CONTENT_EVENT" && sync && scheduler && socket) {
    sync.update(message); scheduler.setVideoTime(sync.videoTime);
    if (message.event === "pause" || message.event === "ended") scheduler.pause();
    if (message.event === "play") scheduler.resume();
    if (message.event === "seeking") { const next = sync.seek(message.currentTime); scheduler.reset(); socket.send({ type: "seek", session: next, media_t0: message.currentTime }); }
    if (message.event === "ad") scheduler.gain.gain.value = 0; else if (!sync.ad) scheduler.gain.gain.value = 1;
  }
});
