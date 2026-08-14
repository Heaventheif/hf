//go:build !postgres

// هذا الملف يُبنى افتراضياً (بدون build tag "postgres") — لا يتطلب أي
// تبعية خارجية، ولذلك يبني المشروع بالكامل فوراً بدون إنترنت.
//
// لتفعيل تخزين NeonDB/Postgres حقيقي:
//  1. go get github.com/jackc/pgx/v5/pgxpool
//  2. go mod tidy
//  3. ابنِ بالوسم:  go build -tags postgres .
//
// عندها يُستخدم postgres_real.go تلقائياً بدل هذا الملف.
package session

import "errors"

// newPostgresStore هنا مجرد "بوابة" — تُعيد خطأ فوراً فتتراجع New()
// تلقائياً إلى المخزن في الذاكرة (memoryStore)، بفلسفة "fail soft".
func newPostgresStore(dsn, collection string) (Store, error) {
	return nil, errors.New("postgres support not compiled in — build with -tags postgres")
}
