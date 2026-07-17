// Package plugins يحمل العقد العام الوحيد الذي تلتزم به أي خدمة في هذا
// المشروع — قديمة كانت (بعد ترحيلها) أو جديدة تُضاف مستقبلاً. هذا يحلّ
// محل internal/registry (init() + blank import) الذي كان يُستخدَم سابقاً:
// التسجيل الآن سطر صريح في main.go بدل blank import صامت الفشل — وهذا
// تحديداً يمنع تكرار خطأ "نسيان تسجيل plugin" (كان سبب غياب img_tr سابقاً).
package plugins

import "net/http"

// Route يمثل مساراً واحداً: method+pattern (بصيغة net/http.ServeMux منذ
// Go 1.22، مثل "POST /gemini") + الـ handler الخاص به.
type Route struct {
	Method  string
	Pattern string
	Handler http.HandlerFunc
}

// Service هو العقد الوحيد الذي تلتزم به أي خدمة في هذا المشروع.
//
// ملاحظة: هذا استثناء موثَّق صراحة لقيد "ممنوع إضافة interfaces" (راجع
// القيود الصارمة) — الهدف من ذلك القيد منع إطار DI/reflection
// auto-wiring، وليس منع عقد توصيف بسيط كهذا. يُسمح حصراً بهذه الواجهة؛
// أي إضافة أخرى من نوع container/framework/lifecycle hook (Init/Shutdown
// على الواجهة نفسها) تبقى ممنوعة تماماً.
type Service interface {
	Name() string // يُستخدم فقط للّوغ ولعرضه في GET /
	Routes() []Route
}
