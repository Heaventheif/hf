//go:build postgres

// هذا الملف هو مخزن الجلسات الدائم عبر NeonDB (Postgres serverless).
// يُبنى فقط عند تفعيل الوسم "postgres"
// (go build -tags postgres)، بعد تشغيل:
//
//	go get github.com/jackc/pgx/v5/pgxpool
//	go mod tidy
//
// (هذا يحدث تلقائياً داخل مرحلة بناء Dockerfile، حيث الاتصال بالإنترنت
// متاح وقت البناء.)
//
// ملاحظة NeonDB: الاتصالات الخاملة تُنام (serverless auto-suspend)،
// لذلك نضبط ConnectTimeout أعلى من المعتاد (~8 ثوانٍ) لإعطاء وقت كافٍ
// لـ "cold start" لقاعدة البيانات عند أول اتصال بعد فترة خمول.
package session

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type postgresStore struct {
	pool  *pgxpool.Pool
	table string
}

var safeIdentRe = regexp.MustCompile(`[^a-zA-Z0-9_]+`)

// safeTableName يشتق اسم جدول آمناً من اسم الـ collection عبر إزالة أي
// حرف ليس حرفاً/رقماً/شرطة سفلية، لتفادي أي احتمال SQL injection عبر
// اسم الجدول (لا يمكن استخدام placeholders لأسماء الجداول في pgx).
func safeTableName(collection string) string {
	clean := safeIdentRe.ReplaceAllString(collection, "")
	if clean == "" {
		clean = "default"
	}
	return "sessions_" + clean
}

func newPostgresStore(dsn, collection string) (Store, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()

	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("postgres: invalid DSN: %w", err)
	}
	cfg.ConnConfig.ConnectTimeout = 8 * time.Second

	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("postgres: pool init failed: %w", err)
	}

	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("postgres: ping failed: %w", err)
	}

	table := safeTableName(collection)
	createSQL := fmt.Sprintf(`CREATE TABLE IF NOT EXISTS %s (
		thread_id TEXT PRIMARY KEY,
		messages JSONB NOT NULL DEFAULT '[]'::jsonb,
		updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
	)`, table)
	if _, err := pool.Exec(ctx, createSQL); err != nil {
		pool.Close()
		return nil, fmt.Errorf("postgres: create table failed: %w", err)
	}

	return &postgresStore{pool: pool, table: table}, nil
}

func (p *postgresStore) Load(ctx context.Context, threadID string) ([]Message, error) {
	query := fmt.Sprintf(`SELECT messages FROM %s WHERE thread_id = $1`, p.table)
	var raw []byte
	err := p.pool.QueryRow(ctx, query, threadID).Scan(&raw)
	if errors.Is(err, pgx.ErrNoRows) {
		return []Message{}, nil
	}
	if err != nil {
		return nil, err
	}
	var msgs []Message
	if err := json.Unmarshal(raw, &msgs); err != nil {
		return nil, err
	}
	return lastN(msgs, historyLimit()), nil
}

func (p *postgresStore) Save(ctx context.Context, threadID string, messages []Message) error {
	trimmed := lastN(messages, historyLimit())
	raw, err := json.Marshal(trimmed)
	if err != nil {
		return err
	}
	query := fmt.Sprintf(`
		INSERT INTO %s (thread_id, messages, updated_at)
		VALUES ($1, $2, now())
		ON CONFLICT (thread_id) DO UPDATE
		SET messages = EXCLUDED.messages, updated_at = now()
	`, p.table)
	_, err = p.pool.Exec(ctx, query, threadID, raw)
	return err
}

func (p *postgresStore) Clear(ctx context.Context, threadID string) error {
	query := fmt.Sprintf(`DELETE FROM %s WHERE thread_id = $1`, p.table)
	_, err := p.pool.Exec(ctx, query, threadID)
	return err
}
