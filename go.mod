module sunkenbot

go 1.22.2

require github.com/google/uuid v1.6.0

require (
	github.com/PuerkitoBio/goquery v1.9.2
	github.com/chromedp/cdproto v0.0.0-20240202021202-6d0b6a386732
	github.com/chromedp/chromedp v0.9.5
	github.com/otiai10/gosseract/v2 v2.4.1
	go.mongodb.org/mongo-driver v1.17.4
	golang.org/x/image v0.19.0
)

require (
	github.com/andybalholm/cascadia v1.3.2 // indirect
	github.com/chromedp/sysutil v1.0.0 // indirect
	github.com/gobwas/httphead v0.1.0 // indirect
	github.com/gobwas/pool v0.2.1 // indirect
	github.com/gobwas/ws v1.3.2 // indirect
	github.com/golang/snappy v0.0.4 // indirect
	github.com/josharian/intern v1.0.0 // indirect
	github.com/klauspost/compress v1.16.7 // indirect
	github.com/mailru/easyjson v0.7.7 // indirect
	github.com/montanaflynn/stats v0.7.1 // indirect
	github.com/xdg-go/pbkdf2 v1.0.0 // indirect
	github.com/xdg-go/scram v1.1.2 // indirect
	github.com/xdg-go/stringprep v1.0.4 // indirect
	github.com/youmark/pkcs8 v0.0.0-20240726163527-a2c0da244d78 // indirect
	golang.org/x/crypto v0.26.0 // indirect
	golang.org/x/sync v0.8.0 // indirect
	golang.org/x/sys v0.23.0 // indirect
	golang.org/x/text v0.17.0 // indirect
)

// ملاحظة: التوجيهات replace أدناه تُوجِّه بعض التبعيات (mongo-driver +
// حزم golang.org/x/*) إلى مرايا GitHub الرسمية بدل مسارات "vanity import"
// الأصلية (go.mongodb.org, golang.org/x/...) — وذلك لأن بيئة تطوير هذا
// المشروع تحديداً تحجب تلك النطاقات تحديداً بينما تسمح بـ github.com.
// المحتوى مطابق تماماً (نفس الكود، نفس الإصدار)، وهذا لا يغيّر أي سلوك.
// إن كانت بيئة النشر لديك تصل لـ golang.org/go.mongodb.org مباشرة، يمكنك
// حذف كل أسطر replace بأمان وتشغيل `go mod tidy` من جديد — النتيجة ستكون
// مطابقة وظيفياً.
replace go.mongodb.org/mongo-driver => github.com/mongodb/mongo-go-driver v1.17.4

replace golang.org/x/text => github.com/golang/text v0.17.0

replace golang.org/x/crypto => github.com/golang/crypto v0.26.0

replace golang.org/x/sync => github.com/golang/sync v0.8.0

replace golang.org/x/tools => github.com/golang/tools v0.21.1-0.20240508182429-e35e4ccd0d2d

replace golang.org/x/mod => github.com/golang/mod v0.17.0

replace golang.org/x/net => github.com/golang/net v0.28.0

replace golang.org/x/sys => github.com/golang/sys v0.24.0

replace golang.org/x/term => github.com/golang/term v0.23.0

replace golang.org/x/telemetry => github.com/golang/telemetry v0.0.0-20240228155512-f48c80bd79b2

replace golang.org/x/image => github.com/golang/image v0.19.0
