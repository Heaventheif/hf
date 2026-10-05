const $ = (id) => document.getElementById(id);
const settings = await chrome.storage.local.get({ spaceUrl: "https://kiyunhai-s.hf.space" });
$("spaceUrl").value = settings.spaceUrl;
const setStatus = (text) => $("status").textContent = text;

$("start").onclick = async () => {
  await chrome.storage.local.set({ spaceUrl: $("spaceUrl").value.trim() });
  setStatus("جارٍ إيقاظ الخادم والاتصال…");
  const response = await chrome.runtime.sendMessage({ type: "START" });
  if (!response?.ok) setStatus(`خطأ: ${response?.error || "تعذر التشغيل"}`); else setStatus("تم التشغيل؛ شغّل الفيديو الآن");
};
$("stop").onclick = async () => { await chrome.runtime.sendMessage({ type: "STOP" }); setStatus("تم الإيقاف"); };
chrome.runtime.onMessage.addListener((message) => { if (message.type === "OFFSCREEN_STATUS") setStatus(`${message.state}${message.detail ? ` — ${message.detail}` : ""}`); });
