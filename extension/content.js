(() => {
  let video = null;
  let lastTime = 0;
  let timer = null;

  const findVideo = () => document.querySelector("video");
  const send = (payload) => chrome.runtime.sendMessage({ type: "CONTENT_EVENT", ...payload }).catch(() => {});

  function bind() {
    const next = findVideo();
    if (!next || next === video) return;
    if (video) ["play", "pause", "seeking", "ended"].forEach((event) => video.removeEventListener(event, onVideoEvent));
    video = next;
    ["play", "pause", "seeking", "ended"].forEach((event) => video.addEventListener(event, onVideoEvent));
  }

  function onVideoEvent(event) {
    send({ event: event.type, currentTime: video?.currentTime || 0, playbackRate: video?.playbackRate || 1, videoId: new URL(location.href).searchParams.get("v") || location.pathname });
  }

  timer = setInterval(() => {
    bind();
    if (!video) return;
    const ad = !!document.querySelector(".ad-showing, #movie_player.ad-interrupting");
    if (Math.abs(video.currentTime - lastTime) > 2 || ad) onVideoEvent({ type: ad ? "ad" : "timeupdate" });
    lastTime = video.currentTime;
  }, 250);

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === "GET_VIDEO_STATE") {
      sendResponse({ currentTime: video?.currentTime || 0, playbackRate: video?.playbackRate || 1 });
      return true;
    }
  });
})();
