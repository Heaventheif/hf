//go:build mongo

// هذا الملف هو المكافئ الحقيقي لـ:
//
//	from motor.motor_asyncio import AsyncIOMotorClient
//	client["sunken"][collection]
//
// في plugins/gemini.py و plugins/groq.py.
//
// يُبنى فقط عند تفعيل الوسم "mongo" (go build -tags mongo)، بعد تشغيل:
//
//	go get go.mongodb.org/mongo-driver/mongo
//	go mod tidy
//
// (تعذّر تحميل هذه المكتبة واختبارها هنا بسبب عزل الشبكة في بيئة
// التطوير الحالية — راجع تعليق أعلى session.go لمزيد من التفاصيل.
// الكود أدناه يتبع التوثيق الرسمي لـ mongo-go-driver ولا يحتاج تعديلاً
// عادةً، لكن يُستحسن تشغيل الاختبارات محلياً بعد التفعيل.)
package session

import (
	"context"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"
)

type mongoStore struct {
	col *mongo.Collection
}

type sessionDoc struct {
	ID        string    `bson:"_id"`
	Messages  []Message `bson:"messages"`
	UpdatedAt time.Time `bson:"updated_at"`
}

func newMongoStore(uri, collection string) (Store, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	opts := options.Client().ApplyURI(uri).SetServerSelectionTimeout(5 * time.Second)
	client, err := mongo.Connect(ctx, opts)
	if err != nil {
		return nil, err
	}
	if err := client.Ping(ctx, nil); err != nil {
		return nil, err
	}

	col := client.Database("sunken").Collection(collection)
	return &mongoStore{col: col}, nil
}

func (m *mongoStore) Load(ctx context.Context, threadID string) ([]Message, error) {
	var doc sessionDoc
	err := m.col.FindOne(ctx, bson.M{"_id": threadID}).Decode(&doc)
	if err == mongo.ErrNoDocuments {
		return []Message{}, nil
	}
	if err != nil {
		return nil, err
	}
	return lastN(doc.Messages, maxHistory), nil
}

func (m *mongoStore) Save(ctx context.Context, threadID string, messages []Message) error {
	_, err := m.col.UpdateOne(ctx,
		bson.M{"_id": threadID},
		bson.M{"$set": bson.M{
			"messages":   lastN(messages, maxHistory),
			"updated_at": time.Now().UTC(),
		}},
		options.Update().SetUpsert(true),
	)
	return err
}

func (m *mongoStore) Clear(ctx context.Context, threadID string) error {
	_, err := m.col.DeleteOne(ctx, bson.M{"_id": threadID})
	return err
}
