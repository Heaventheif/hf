// Package middleware يحتوي المكافئ الدقيق لِـ:
//   - _register_auth_middleware في plugin_loader.py (حماية X-Internal-Token)
//   - CORSMiddleware المضافة في main.py الأصلي
package middleware

import (
	"crypto/subtle"
	"encoding/json"
	"log"
	"net/http"
	"strings"
)

// publicPaths تقابل PUBLIC_PATHS = {"/", "/health"} في بايثون —
// مسارات مستثناة عمداً من حماية التوكن (فحوصات حالة عامة).
var publicPaths = map[string]bool{
	"/":       true,
	"/health": true,
}

// Auth يقابل middleware "http" في FastAPI الذي يفحص X-Internal-Token.
// إن كان internalToken فارغاً (INTERNAL_TOKEN غير مضبوط)، لا حماية —
// نفس السلوك القديم في بايثون، مع نفس التحذير في اللوق.
func Auth(internalToken string, next http.Handler) http.Handler {
	internalToken = strings.TrimSpace(internalToken)

	if internalToken == "" {
		log.Println("⚠️ INTERNAL_TOKEN غير مضبوط — كل الـ endpoints مفتوحة بدون حماية! " +
			"أضف INTERNAL_TOKEN في متغيرات البيئة.")
		return next
	}

	log.Println("🔒 تم تفعيل حماية X-Internal-Token على كل الـ endpoints (عدا / و /health)")

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if publicPaths[r.URL.Path] {
			next.ServeHTTP(w, r)
			return
		}

		supplied := r.Header.Get("X-Internal-Token")
		// مقارنة بزمن ثابت لمنع تسريب معلومات عبر قناة التوقيت (timing side-channel)
		tokensMatch := len(supplied) == len(internalToken) &&
			subtle.ConstantTimeCompare([]byte(supplied), []byte(internalToken)) == 1
		if !tokensMatch {
			clientHost := r.RemoteAddr
			log.Printf("🚫 طلب مرفوض (توكن غير صحيح/مفقود) — %s %s من %s",
				r.Method, r.URL.Path, clientHost)

			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnauthorized)
			_ = json.NewEncoder(w).Encode(map[string]string{
				"status":  "error",
				"message": "Unauthorized — missing or invalid X-Internal-Token",
			})
			return
		}

		next.ServeHTTP(w, r)
	})
}

// CORS يقابل CORSMiddleware(allow_origins=["*"], allow_methods=["GET","POST"], allow_headers=["*"]).
func CORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "*")

		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}

		next.ServeHTTP(w, r)
	})
}
