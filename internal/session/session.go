// Package session هو المكافئ لمنطق تخزين الجلسات المستخدَم في
// plugins/gemini وplugins/groq وplugins/gptx (الثلاثة يشتركون بنفس
// عقد Store: Load/Save/Clear).
//
// التخزين الدائم هنا عبر NeonDB (Postgres serverless)، مفعّل عبر
// build tag "postgres":
//
//	go get github.com/jackc/pgx/v5/pgxpool
//	go mod tidy
//	go build -tags postgres .
//
// (هذا يحدث تلقائياً داخل Dockerfile وقت البناء). بدون هذا الوسم، أو
// بدون ضبط DATABASE_URL/NEON_DATABASE_URL، يعمل المشروع بمخزن في
// الذاكرة (in-memory) فقط — يعمل فوراً بدون أي تبعية خارجية، لكن غير
// دائم عبر إعادة التشغيل.
package session

import (
	"context"
	"log"
	"os"
	"strconv"
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

const defaultMaxHistory = 40 // كان 10 سابقاً — رُفع لأن محادثات المجموعات
// (عدة متحدثين بنفس thread_id) تستهلك نافذة الذاكرة أسرع من محادثة فردية.
// قابل للتهيئة عبر SESSION_HISTORY_LIMIT.

// historyLimit يقرأ SESSION_HISTORY_LIMIT (رقم صحيح موجب) إن كان مضبوطاً
// وصالحاً، وإلا يرجع defaultMaxHistory.
func historyLimit() int {
	v := os.Getenv("SESSION_HISTORY_LIMIT")
	if v == "" {
		return defaultMaxHistory
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		return defaultMaxHistory
	}
	return n
}

// memoryStore هي الحالة الافتراضية عندما لا يوجد DATABASE_URL/
// NEON_DATABASE_URL (أو يفشل الاتصال) — تحتفظ بالجلسات في الذاكرة
// طالما العملية شغّالة، بدون أي تبعية خارجية، لكن غير دائمة عبر إعادة
// التشغيل.
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
	return lastN(msgs, historyLimit()), nil
}

func (m *memoryStore) Save(_ context.Context, threadID string, messages []Message) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.data[threadID] = lastN(messages, historyLimit())
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

// New تُعيد مخزن Postgres (NeonDB) إن كان DATABASE_URL أو NEON_DATABASE_URL
// مضبوطاً واتصل بنجاح، وإلا مخزن في الذاكرة (فلسفة "fail soft").
//
// ملاحظة: التراجع للذاكرة عند فشل newPostgresStore (سواء لعدم بناء
// الملف بوسم "postgres"، أو لفشل اتصال حقيقي) يصدر دائماً سطر تحذير
// واضح في اللوق بدل الصمت.
//
// collection: اسم المجموعة، يقابل ["sunken"]["gemini_sessions"] مثلاً.
func New(collection string) Store {
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		dsn = os.Getenv("NEON_DATABASE_URL")
	}
	if dsn == "" {
		return newMemoryStore()
	}
	store, err := newPostgresStore(dsn, collection)
	if err != nil {
		log.Printf("⚠️ [session:%s] DATABASE_URL/NEON_DATABASE_URL مضبوط لكن الاتصال فشل — "+
			"سيُستخدم مخزن الذاكرة (غير دائم، يُفقد عند إعادة التشغيل): %v", collection, err)
		return newMemoryStore()
	}
	log.Printf("✅ [session:%s] متصل بـ NeonDB (Postgres) — الجلسات ستكون دائمة عبر إعادة التشغيل", collection)
	return store
}
