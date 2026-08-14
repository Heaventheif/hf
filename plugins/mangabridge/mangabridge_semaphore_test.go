package mangabridge

import (
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// TestConcurrencySemaphore_LimitsParallelScrapes يثبت أن الحارس الجديد
// (v4، الجزء و) يمنع فعلياً أكثر من concurrentScrapes عملية "ثقيلة" من
// العمل في آن واحد — دون الحاجة لتشغيل Chromium حقيقي: نحاكي فقط سلوك
// الحارس نفسه (نفس القناة sem التي يستخدمها scrapeChapter فعلياً).
func TestConcurrencySemaphore_LimitsParallelScrapes(t *testing.T) {
	s := New()

	const workers = 8
	var current int32
	var maxObserved int32
	var wg sync.WaitGroup

	for range workers {
		wg.Go(func() {

			s.sem <- struct{}{}
			defer func() { <-s.sem }()

			n := atomic.AddInt32(&current, 1)
			for {
				m := atomic.LoadInt32(&maxObserved)
				if n <= m || atomic.CompareAndSwapInt32(&maxObserved, m, n) {
					break
				}
			}
			time.Sleep(20 * time.Millisecond)
			atomic.AddInt32(&current, -1)
		})
	}
	wg.Wait()

	if maxObserved > concurrentScrapes {
		t.Fatalf("observed %d concurrent scrapes, want <= %d (semaphore cap)", maxObserved, concurrentScrapes)
	}
	if maxObserved != concurrentScrapes {
		t.Logf("observed max concurrency %d (cap is %d) — test still valid, just didn't saturate the cap this run", maxObserved, concurrentScrapes)
	}
}

// TestNew_SemaphoreCapacityMatchesConcurrentScrapes يثبت أن New() تبني
// القناة فعلاً بالسعة concurrentScrapes، لا سعة عشوائية أو صفرية (قناة
// بسعة صفر كانت ستمنع أي عمليتين من العمل حتى بالتتابع الصحيح).
func TestNew_SemaphoreCapacityMatchesConcurrentScrapes(t *testing.T) {
	s := New()
	if cap(s.sem) != concurrentScrapes {
		t.Fatalf("cap(sem) = %d, want %d", cap(s.sem), concurrentScrapes)
	}
}
