// Package shared يوفّر http.Client مشتركاً بمهلة 30 ثانية — يُستخدَم فقط
// من الخدمات التي كان عميلها المحلي الحالي فعلاً 30s (راجع جدول قرار
// http.Client في برومبت إعادة الهيكلة). لا يُستبدَل به أي عميل بمهلة
// مختلفة تلقائياً.
package shared

import (
	"net/http"
	"time"
)

var Client = &http.Client{Timeout: 30 * time.Second}
