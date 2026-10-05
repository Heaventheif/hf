export class SyncManager {
  constructor() { this.session = 1; this.mediaT0 = 0; this.videoTime = 0; this.playbackRate = 1; this.ad = false; }
  start(mediaT0 = 0) { this.session = 1; this.mediaT0 = mediaT0; this.videoTime = mediaT0; }
  seek(time) { this.session += 1; this.mediaT0 = Number(time) || 0; this.videoTime = this.mediaT0; return this.session; }
  update(event) { this.videoTime = Number(event.currentTime ?? this.videoTime); this.playbackRate = Number(event.playbackRate ?? 1); this.ad = event.event === "ad"; }
}
