const OFFSCREEN_URL = "offscreen.html";

async function ensureOffscreen() {
  const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (!contexts.length) {
    await chrome.offscreen.createDocument({
      url: OFFSCREEN_URL,
      reasons: ["USER_MEDIA"],
      justification: "Capture YouTube tab audio and play Arabic dubbing outside the tab"
    });
  }
}

async function activeYouTubeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !/^https:\/\/(m\.)?(www\.)?youtube\.com\//.test(tab.url || "")) {
    throw new Error("افتح فيديو YouTube أولاً");
  }
  return tab;
}

async function start(tabId) {
  const tab = tabId ? await chrome.tabs.get(tabId) : await activeYouTubeTab();
  await ensureOffscreen();
  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  const settings = await chrome.storage.local.get({ spaceUrl: "https://kiyunhai-s.hf.space" });
  const state = await chrome.tabs.sendMessage(tab.id, { type: "GET_VIDEO_STATE" }).catch(() => ({}));
  await chrome.runtime.sendMessage({ type: "OFFSCREEN_START", streamId, tabId: tab.id, spaceUrl: settings.spaceUrl, mediaT0: state?.currentTime || 0 });
  return { ok: true };
}

async function stop() {
  await chrome.runtime.sendMessage({ type: "OFFSCREEN_STOP" });
  const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (contexts.length) await chrome.offscreen.closeDocument();
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (message.type === "START") return sendResponse(await start(message.tabId));
    if (message.type === "STOP") { await stop(); return sendResponse({ ok: true }); }
    if (message.type === "CONTENT_EVENT" && !message.forwarded) {
      const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
      if (contexts.length) await chrome.runtime.sendMessage({ ...message, forwarded: true });
      return sendResponse({ ok: true });
    }
    if (message.type === "OFFSCREEN_STATUS") return sendResponse({ ok: true });
    sendResponse({ ok: false, error: "unknown message" });
  })().catch((error) => sendResponse({ ok: false, error: error.message }));
  return true;
});

chrome.tabs.onRemoved.addListener((tabId) => chrome.runtime.sendMessage({ type: "TAB_CLOSED", tabId }).catch(() => {}));
