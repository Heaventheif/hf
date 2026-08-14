package comic

import (
	"context"
	"net/http"

	"sunkenbot/internal/httpx"
	"sunkenbot/internal/netguard"
	"sunkenbot/internal/plugins"
	"sunkenbot/internal/strutil"
)

const Description = "Comic (arcomixverse.blogspot.com) — استخراج data-label وروابط صور الفصل، بدل cheerio.load() في JS"

const (
	maxPageBytes = 5 * 1024 * 1024 // صفحة سلسلة أو فصل عادية أصغر بكثير من هذا
)

type Service struct {
	client *http.Client
}

func New(client *http.Client) *Service {
	return &Service{client: client}
}

func (s *Service) Name() string { return "comic" }

func (s *Service) Routes() []plugins.Route {
	return []plugins.Route{
		{Method: "POST", Pattern: "/comic/label", Handler: httpx.WrapJSON(s.handleLabel)},
		{Method: "POST", Pattern: "/comic/images", Handler: httpx.WrapJSON(s.handleImages)},
	}
}

type LabelRequest struct {
	SeriesURL string `json:"series_url"`
}

type LabelResponse struct {
	Label string `json:"label"`
}

func (s *Service) handleLabel(ctx context.Context, req LabelRequest) (LabelResponse, error) {
	if req.SeriesURL == "" {
		return LabelResponse{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "series_url مطلوب"},
		}
	}
	raw, err := netguard.SafeFetch(ctx, s.client, req.SeriesURL, maxPageBytes)
	if err != nil {
		return LabelResponse{}, &httpx.HTTPError{
			Code: http.StatusServiceUnavailable,
			Body: map[string]any{"error": "تعذّر جلب صفحة السلسلة: " + strutil.Truncate(err.Error(), 150)},
		}
	}
	label, ok := extractMangaLabel(string(raw))
	if !ok {
		return LabelResponse{}, &httpx.HTTPError{
			Code: http.StatusNotFound,
			Body: map[string]any{"error": "لم يُعثر على data-label في هذه الصفحة"},
		}
	}
	return LabelResponse{Label: label}, nil
}

type ImagesRequest struct {
	ChapterURL string `json:"chapter_url"`
}

type ImagesResponse struct {
	Images []string `json:"images"`
}

func (s *Service) handleImages(ctx context.Context, req ImagesRequest) (ImagesResponse, error) {
	if req.ChapterURL == "" {
		return ImagesResponse{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "chapter_url مطلوب"},
		}
	}
	raw, err := netguard.SafeFetch(ctx, s.client, req.ChapterURL, maxPageBytes)
	if err != nil {
		return ImagesResponse{}, &httpx.HTTPError{
			Code: http.StatusServiceUnavailable,
			Body: map[string]any{"error": "تعذّر جلب صفحة الفصل: " + strutil.Truncate(err.Error(), 150)},
		}
	}
	images := extractChapterImages(string(raw))
	return ImagesResponse{Images: images}, nil
}
