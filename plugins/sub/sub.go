// Package sub يقابل plugins/sub.py بالكامل: إضافة ترجمة نصية (SRT + libass)
// على مقاطع فيديو، مع تحكم بموضع كل سطر عمودياً، ومعالجة في الخلفية
// (goroutine تقابل BackgroundTasks في FastAPI).
package sub

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"

	"sunkenbot/internal/httpx"
	"sunkenbot/internal/netguard"
	"sunkenbot/internal/plugins"
	"sunkenbot/internal/strutil"
)

const Description = "إضافة ترجمة نصية (ثابتة أو زمنية) على مقاطع الفيديو مع تحكم بموضع كل سطر عمودياً"

// اسم عائلة الخط العربي المستخدم في الحرق (يجب أن يكون مثبتاً فعلياً في بيئة التشغيل).
const arabicFontFamily = "Noto Naskh Arabic"

const defaultPosition = 4

// yPositionPercent تقابل Y_POSITION_PERCENT في بايثون.
var yPositionPercent = map[int]float64{
	1: 0.10,
	2: 0.30,
	3: 0.50,
	4: 0.75,
	5: 0.90,
}

// jobTTL: أقصى عمر لأي وظيفة (وملفاتها) لم تُحمَّل عبر /subtitler/download —
// يمنع تراكم ملفات الفيديو ومدخلات jobs في الذاكرة إلى الأبد (نفس نمط
// التنظيف الدوري المطبَّق في plugins/mangabridge).
const jobTTL = 30 * time.Minute

// execTimeout: أقصى مدة تشغيل لأي عملية ffprobe/ffmpeg فرعية، لمنع تعليق
// أبدي في حال ملف فيديو تالف أو ضخم بشكل غير طبيعي.
const execTimeout = 5 * time.Minute

// maxDownloadBytes: أقصى حجم لفيديو يُقبل تحميله من video_url، لتفادي
// استنزاف الذاكرة/القرص عبر روابط تشير لملفات ضخمة جداً.
const maxDownloadBytes = 300 * 1024 * 1024 // 300MB

// Service يحمل عميل http.Client (60 ثانية — محلي، راجع جدول قرار
// http.Client) وحالة jobs في الذاكرة (jobsMu/jobs، كانت متغيرات حزمة
// عامة) كحقول صريحة.
type Service struct {
	client *http.Client

	jobsMu sync.Mutex
	jobs   map[string]*job
}

func New(client *http.Client) *Service {
	return &Service{client: client, jobs: map[string]*job{}}
}

func (s *Service) Name() string { return "sub" }

func (s *Service) Routes() []plugins.Route {
	return []plugins.Route{
		{Method: "POST", Pattern: "/subtitler/create", Handler: httpx.Handle(s.handleCreate)},
		{Method: "GET", Pattern: "/subtitler/status/{job_id}", Handler: httpx.Handle(s.handleStatus)},
		{Method: "GET", Pattern: "/subtitler/download/{job_id}", Handler: s.handleDownload},
	}
}

// ─── أنواع الطلب ────────────────────────────────────────────────────

// SubtitleCue تقابل SubtitleCue(BaseModel) في بايثون.
type SubtitleCue struct {
	Position int      `json:"position"`
	Start    *float64 `json:"start"`
	End      *float64 `json:"end"`
	Text     string   `json:"text"`
}

// SubtitleRequest تقابل SubtitleRequest(BaseModel).
type SubtitleRequest struct {
	VideoURL string        `json:"video_url"`
	Cues     []SubtitleCue `json:"cues"`
}

// ─── حالة العمليات في الذاكرة (تقابل _sub_jobs) ─────────────────────

type jobStatus string

const (
	jobPending jobStatus = "pending"
	jobDone    jobStatus = "done"
	jobError   jobStatus = "error"
)

type job struct {
	Status       jobStatus
	ResultPath   string
	RelatedFiles []string
	Reason       string
	CreatedAt    time.Time
}

func (s *Service) setJob(id string, j *job) {
	s.jobsMu.Lock()
	defer s.jobsMu.Unlock()
	if j.CreatedAt.IsZero() {
		if existing, ok := s.jobs[id]; ok {
			j.CreatedAt = existing.CreatedAt // حافظ على وقت الإنشاء الأصلي عند تحديث حالة عمل موجود
		} else {
			j.CreatedAt = time.Now()
		}
	}
	s.jobs[id] = j
}

// cleanupOldJobs يحذف أي وظيفة (وملفاتها المرتبطة على القرص) أقدم من jobTTL
// ولم تُحمَّل بعد عبر /subtitler/download — يمنع تسرّب الذاكرة والقرص لو
// نسي المستخدم استدعاء endpoint التحميل. يُستدعى بشكل كسول عند كل طلب إنشاء جديد.
func (s *Service) cleanupOldJobs() {
	s.jobsMu.Lock()
	defer s.jobsMu.Unlock()

	cutoff := time.Now().Add(-jobTTL)
	for id, j := range s.jobs {
		if j.CreatedAt.IsZero() || j.CreatedAt.After(cutoff) {
			continue
		}
		for _, f := range append([]string{j.ResultPath}, j.RelatedFiles...) {
			if f != "" {
				_ = os.Remove(f)
			}
		}
		delete(s.jobs, id)
	}
}

func (s *Service) getJob(id string) (*job, bool) {
	s.jobsMu.Lock()
	defer s.jobsMu.Unlock()
	j, ok := s.jobs[id]
	return j, ok
}

func (s *Service) deleteJob(id string) {
	s.jobsMu.Lock()
	defer s.jobsMu.Unlock()
	delete(s.jobs, id)
}

// ─── SRT ─────────────────────────────────────────────────────────────

// srtEscape يعقّم نص الترجمة القادم من المستخدم قبل إدراجه في ملف SRT.
//
// بدون هذا التعقيم، نص مثل:
//
//	"مرحبا}{\pos(0,0)\fscx500}\n\n2\n00:00:05,000 --> 00:00:06,000\nنص محقون"
//
// كان يُنتج مقطع SRT إضافياً كاملاً (index 2) لم يُنشئه الخادم، ويُدخل أوامر
// تنسيق ASS/libass عشوائية (تحريك/تكبير النص) عبر الأقواس المعقوفة الحرفية.
func srtEscape(text string) string {
	// إزالة كل فواصل الأسطر: هي ما يسمح لنص مستخدم واحد بـ"الخروج" من مقطعه
	// وتزييف مقطع SRT جديد بالكامل (رقم + سطر توقيت + نص).
	text = strings.ReplaceAll(text, "\r\n", " ")
	text = strings.ReplaceAll(text, "\r", " ")
	text = strings.ReplaceAll(text, "\n", " ")

	// استبدال الأقواس المعقوفة بمكافئ Unicode بصري مشابه، بدل حذفها، حتى لا
	// يُفسّرها ffmpeg/libass كأوامر override (مثل \pos \fscx \move) مع الحفاظ
	// على نص المستخدم كما هو تقريباً من الناحية المرئية.
	text = strings.ReplaceAll(text, "{", "｛")
	text = strings.ReplaceAll(text, "}", "｝")

	return strings.TrimSpace(text)
}

func secondsToSRTTS(seconds float64) string {
	if seconds < 0 {
		seconds = 0
	}
	hours := int(seconds) / 3600
	minutes := (int(seconds) % 3600) / 60
	secs := int(seconds) % 60
	millis := int((seconds-float64(int(seconds)))*1000 + 0.5)
	return fmt.Sprintf("%02d:%02d:%02d,%03d", hours, minutes, secs, millis)
}

func buildSRT(cues []SubtitleCue, videoDuration float64, width, height int) string {
	var b strings.Builder
	xCenter := width / 2

	for i, cue := range cues {
		yPercent, ok := yPositionPercent[cue.Position]
		if !ok {
			yPercent = yPositionPercent[defaultPosition]
		}
		y := int(float64(height) * yPercent)

		startSec := 0.0
		if cue.Start != nil {
			startSec = *cue.Start
		}
		endSec := videoDuration
		if cue.End != nil {
			endSec = *cue.End
		}
		if endSec > videoDuration {
			endSec = videoDuration
		}

		startTS := secondsToSRTTS(startSec)
		endTS := secondsToSRTTS(endSec)

		text := srtEscape(cue.Text)
		positioned := fmt.Sprintf("{\\an5\\pos(%d,%d)}%s", xCenter, y, text)

		fmt.Fprintf(&b, "%d\n%s --> %s\n%s\n\n", i+1, startTS, endTS, positioned)
	}
	return b.String()
}

// ─── ffprobe ─────────────────────────────────────────────────────────

func probeVideoInfo(videoPath string) (duration float64, width, height int, err error) {
	ctx, cancel := context.WithTimeout(context.Background(), execTimeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, "ffprobe", "-v", "error",
		"-select_streams", "v:0",
		"-show_entries", "stream=width,height:format=duration",
		"-of", "default=noprint_wrappers=1",
		videoPath,
	)
	out, err := cmd.Output()
	if ctx.Err() == context.DeadlineExceeded {
		return 0, 0, 0, fmt.Errorf("ffprobe: تجاوز الوقت المسموح (%s)", execTimeout)
	}
	if err != nil {
		return 0, 0, 0, fmt.Errorf("ffprobe: %w", err)
	}

	info := map[string]string{}
	for line := range strings.SplitSeq(strings.TrimSpace(string(out)), "\n") {
		parts := strings.SplitN(line, "=", 2)
		if len(parts) == 2 {
			info[strings.TrimSpace(parts[0])] = strings.TrimSpace(parts[1])
		}
	}

	duration, _ = strconv.ParseFloat(info["duration"], 64)
	w, _ := strconv.ParseFloat(info["width"], 64)
	h, _ := strconv.ParseFloat(info["height"], 64)
	width, height = int(w), int(h)

	if width == 0 || height == 0 {
		return 0, 0, 0, fmt.Errorf("تعذّر قراءة أبعاد الفيديو عبر ffprobe")
	}

	duration -= 0.05
	if duration < 0.1 {
		duration = 0.1
	}
	return duration, width, height, nil
}

// ─── المعالجة الفعلية (تعمل في goroutine خلفية) ──────────────────────

func (s *Service) processVideoSubtitles(jobID, videoURL string, cues []SubtitleCue) {
	uniqueID := uuid.New().String()
	inputVideo := fmt.Sprintf("input_%s.mp4", uniqueID)
	outputVideo := fmt.Sprintf("output_%s.mp4", uniqueID)
	srtFile := fmt.Sprintf("sub_%s.srt", uniqueID)

	fail := func(reason string) {
		s.setJob(jobID, &job{Status: jobError, Reason: reason})
		for _, f := range []string{inputVideo, srtFile, outputVideo} {
			_ = os.Remove(f)
		}
	}

	// 1. تحميل الفيديو — يُكتب مباشرة على القرص عبر io.Copy بدل تحميله
	// بالكامل في الذاكرة أولاً، مع حد أقصى للحجم (maxDownloadBytes) لمنع
	// استنزاف الذاكرة/القرص عبر رابط يشير لملف ضخم بشكل غير طبيعي.
	resp, err := netguard.SafeFetchStream(context.Background(), s.client, videoURL)
	if err != nil {
		fail("رابط الفيديو غير مسموح أو تعذّر تحميله: " + err.Error())
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		fail("فشل تحميل الفيديو من الرابط الموفر")
		return
	}

	out, err := os.Create(inputVideo)
	if err != nil {
		fail("فشل إنشاء ملف الفيديو محلياً")
		return
	}
	limited := io.LimitReader(resp.Body, maxDownloadBytes+1)
	written, copyErr := io.Copy(out, limited)
	_ = out.Close()
	if copyErr != nil {
		_ = os.Remove(inputVideo)
		fail("فشل قراءة محتوى الفيديو")
		return
	}
	if written > maxDownloadBytes {
		_ = os.Remove(inputVideo)
		fail(fmt.Sprintf("الفيديو يتجاوز الحد الأقصى المسموح (%d MB)", maxDownloadBytes/(1024*1024)))
		return
	}

	// 2. أبعاد ومدة الفيديو
	duration, width, height, err := probeVideoInfo(inputVideo)
	if err != nil {
		fail(err.Error())
		return
	}

	// 3. بناء SRT
	srtContent := buildSRT(cues, duration, width, height)
	if srtContent == "" {
		fail("فشل بناء ملف الترجمة — لا توجد مقاطع صالحة")
		return
	}
	if err := os.WriteFile(srtFile, []byte(srtContent), 0644); err != nil {
		fail("فشل كتابة ملف الترجمة")
		return
	}

	// 4. حرق الترجمة عبر ffmpeg + libass
	vf := fmt.Sprintf("subtitles=%s:original_size=%dx%d:force_style='FontName=%s,FontSize=20'",
		srtFile, width, height, arabicFontFamily)

	ctx, cancel := context.WithTimeout(context.Background(), execTimeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, "ffmpeg", "-y", "-i", inputVideo,
		"-vf", vf, "-c:a", "copy", "-preset", "ultrafast", outputVideo)
	stderr, err := cmd.CombinedOutput()
	if ctx.Err() == context.DeadlineExceeded {
		fail(fmt.Sprintf("فشل ffmpeg: تجاوز الوقت المسموح (%s)", execTimeout))
		return
	}
	if err != nil {
		fail("فشل ffmpeg: " + strutil.Truncate(string(stderr), 300))
		return
	}

	s.setJob(jobID, &job{
		Status:       jobDone,
		ResultPath:   outputVideo,
		RelatedFiles: []string{inputVideo, srtFile},
	})
}

// ─── HTTP handlers ───────────────────────────────────────────────────

func (s *Service) handleCreate(r *http.Request) (map[string]any, error) {
	s.cleanupOldJobs()

	var req SubtitleRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		return nil, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "طلب غير صالح: " + err.Error()},
		}
	}
	if req.VideoURL == "" || len(req.Cues) == 0 {
		return nil, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "video_url و cues مطلوبان"},
		}
	}
	for i := range req.Cues {
		if req.Cues[i].Position == 0 {
			req.Cues[i].Position = defaultPosition
		}
	}

	jobID := "sub_" + uuid.New().String()[:12]
	s.setJob(jobID, &job{Status: jobPending})

	go s.processVideoSubtitles(jobID, req.VideoURL, req.Cues)

	return map[string]any{"job_id": jobID, "status": "pending"}, nil
}

func (s *Service) handleStatus(r *http.Request) (map[string]any, error) {
	jobID := r.PathValue("job_id")
	j, ok := s.getJob(jobID)
	if !ok {
		return nil, &httpx.HTTPError{
			Code: http.StatusNotFound,
			Body: map[string]any{"detail": "العملية غير موجودة"},
		}
	}

	switch j.Status {
	case jobDone:
		return map[string]any{
			"status":       "done",
			"download_url": "/subtitler/download/" + jobID,
		}, nil
	case jobError:
		reason := j.Reason
		if reason == "" {
			reason = "خطأ غير معروف"
		}
		return map[string]any{"status": "error", "reason": reason}, nil
	default:
		return map[string]any{"status": "pending"}, nil
	}
}

// handleDownload يبث ملف الفيديو مباشرة (io.Copy + رؤوس Content-*) ثم
// يحذف الملفات المؤقتة — لا يلائم شكل httpx.Handle[Res] العام (لا يوجد
// جسم JSON للرد الناجح)، فيبقى http.HandlerFunc عادياً في Routes().
func (s *Service) handleDownload(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("job_id")
	j, ok := s.getJob(jobID)
	if !ok || j.Status != jobDone {
		httpx.JSON(w, http.StatusNotFound, map[string]any{"detail": "الملف غير جاهز أو غير موجود"})
		return
	}

	f, err := os.Open(j.ResultPath)
	if err != nil {
		httpx.JSON(w, http.StatusNotFound, map[string]any{"detail": "الملف غير جاهز أو غير موجود"})
		return
	}
	defer f.Close()

	w.Header().Set("Content-Type", "video/mp4")
	w.Header().Set("Content-Disposition", `attachment; filename="subtitled_video.mp4"`)
	_, _ = io.Copy(w, f)

	// تنظيف الملفات المؤقتة بعد الإرسال — يقابل __del__ في
	// DeleteOnCloseFileResponse في بايثون.
	for _, path := range append([]string{j.ResultPath}, j.RelatedFiles...) {
		_ = os.Remove(path)
	}
	s.deleteJob(jobID)
}
