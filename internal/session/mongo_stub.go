//go:build !mongo

// هذا الملف يُبنى افتراضياً (بدون build tag "mongo") — لا يتطلب أي
// تبعية خارجية، ولذلك يبني المشروع بالكامل فوراً بدون إنترنت.
//
// لتفعيل تخزين MongoDB حقيقي (مطابق لـ motor في نسخة بايثون):
//  1. go get go.mongodb.org/mongo-driver/mongo
//  2. go mod tidy
//  3. ابنِ بالوسم:  go build -tags mongo .
//
// عندها يُستخدم mongo_real.go تلقائياً بدل هذا الملف.
package session

import "errors"

// newMongoStore هنا مجرد "بوابة" — تُعيد خطأ فوراً فتتراجع New() تلقائياً
// إلى المخزن في الذاكرة (memoryStore)، تماماً كما كانت بايثون تتراجع
// بهدوء عند فشل الاتصال بـ Mongo.
func newMongoStore(uri, collection string) (Store, error) {
	return nil, errors.New("mongo support not compiled in — build with -tags mongo")
}
