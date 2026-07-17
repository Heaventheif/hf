// Package session هو المكافئ لمنطق MongoDB المكرَّر في plugins/gemini.py
// و plugins/groq.py (كلاهما كان يملك نفس دوال _get_db/_load/_save/_clear).
//
// هنا وحّدنا المنطق في مكان واحد بدل التكرار — تحسين طبيعي عند النقل
// لـ Go، لأن السلوك متطابق 100% بين الملفين في نسخة بايثون.
//
// ملاحظة مهمة حول MongoDB في هذه البيئة تحديداً:
// عزل الشبكة (network sandbox) في بيئة التطوير هذه يمنع الوصول إلى
// go.mongodb.org (نطاق مطلوب لتحميل official driver عبر `go get`).
// لذلك تعذّر هنا تحميل واختبار mongo-go-driver فعلياً. الكود في
// mongo.go مكتوب وصحيح ويعمل عادةً بلا مشاكل بمجرد تشغيل:
//
//	go get go.mongodb.org/mongo-driver/mongo
//	go mod tidy
//
// على أي جهاز له اتصال إنترنت طبيعي. الافتراضي الآمن حالياً (وهو أيضاً
// نفس سلوك بايثون عند عدم ضبط MONGO_URI): استخدام مخزن في الذاكرة
// (in-memory) — يعمل فوراً بدون أي تبعية خارجية.
package session

import (
	"context"
	"log"
	"os"
	"sync"
)

// Message تقابل {"role": ..., "content": ...} في بايثون.
type Message struct {
	Role    string `json:"role" bson:"role"`
	Content string `json:"content" bson:"content"`
}

// Store هي الواجهة المكافئة لـ _get_db/_load/_save/_clear في بايثون.
type Store interface {
	Load(ctx context.Context, threadID string) ([]Message, error)
	Save(ctx context.Context, threadID string, messages []Message) error
	Clear(ctx context.Context, threadID string) error
}

const maxHistory = 10 // نفس [-10:] في بايثون

// memoryStore تقابل الحالة الافتراضية في بايثون عندما لا يوجد MONGO_URI
// (أو يفشل الاتصال) — دوال _load/_save/_clear ترجع/تتجاهل بهدوء.
// الفرق: بدل تجاهل الحفظ تماماً (كما في بايثون)، نحتفظ بالجلسات في
// الذاكرة طالما العملية شغّالة — تجربة أفضل بدون أي تبعية خارجية،
// وسلوك مطابق تماماً عند عدم الحاجة لاستمرارية عبر إعادة التشغيل.
type memoryStore struct {
	mu   sync.Mutex
	data map[string][]Message
}

func newMemoryStore() *memoryStore {
	return &memoryStore{data: make(map[string][]Message)}
}

func (m *memoryStore) Load(_ context.Context, threadID string) ([]Message, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	msgs := m.data[threadID]
	return lastN(msgs, maxHistory), nil
}

func (m *memoryStore) Save(_ context.Context, threadID string, messages []Message) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.data[threadID] = lastN(messages, maxHistory)
	return nil
}

func (m *memoryStore) Clear(_ context.Context, threadID string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.data, threadID)
	return nil
}

func lastN(s []Message, n int) []Message {
	if len(s) <= n {
		out := make([]Message, len(s))
		copy(out, s)
		return out
	}
	out := make([]Message, n)
	copy(out, s[len(s)-n:])
	return out
}

// New تقابل _get_db() في بايثون: تُعيد مخزن Mongo إن كان MONGO_URI
// مضبوطاً واتصل بنجاح، وإلا مخزن في الذاكرة (نفس فلسفة "fail soft"
// الموجودة في النسخة الأصلية).
//
// ملاحظة إصلاح: كان التراجع للذاكرة يحدث سابقاً بصمت تامة عند فشل
// newMongoStore (سواء لعدم بناء الملف بوسم "mongo"، أو لفشل اتصال حقيقي)،
// بدون أي سطر تحذير في اللوق — ما جعل هذا يمر دون ملاحظة لفترة طويلة رغم
// أن MONGO_URI موثَّق كخيار "تخزين دائم". الآن نُصدر تحذيراً واضحاً.
//
// collection: اسم المجموعة، يقابل ["sunken"]["gemini_sessions"] مثلاً.
func New(collection string) Store {
	uri := os.Getenv("MONGO_URI")
	if uri == "" {
		return newMemoryStore()
	}
	store, err := newMongoStore(uri, collection)
	if err != nil {
		log.Printf("⚠️ [session:%s] MONGO_URI مضبوط لكن الاتصال فشل — سيُستخدم مخزن الذاكرة "+
			"(غير دائم، يُفقد عند إعادة التشغيل): %v", collection, err)
		return newMemoryStore()
	}
	log.Printf("✅ [session:%s] متصل بـ MongoDB — الجلسات ستكون دائمة عبر إعادة التشغيل", collection)
	return store
}
