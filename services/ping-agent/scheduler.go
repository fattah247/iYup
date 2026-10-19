package main

import (
	"context"
	"log"
	"sync"
	"time"
)

// runScheduler runs every check on its own interval until ctx ends. Each
// target has its own loop, so a slow target never pushes back the others'
// schedules; while it runs it holds one of the concurrency slots a shared
// semaphore caps. First checks are spread over up to two seconds so a restart
// does not fire everything simultaneously.
func runScheduler(ctx context.Context, checks []*check, concurrency int, rec *recorder) {
	sem := make(chan struct{}, max(1, concurrency))
	var wg sync.WaitGroup
	for i, c := range checks {
		offset := 2 * time.Second * time.Duration(i) / time.Duration(len(checks))
		wg.Add(1)
		go func() {
			defer wg.Done()
			runLoop(ctx, c, sem, rec, offset)
		}()
	}
	wg.Wait()
}

func runLoop(ctx context.Context, c *check, sem chan struct{}, rec *recorder, offset time.Duration) {
	timer := time.NewTimer(offset)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}
		started := time.Now()
		select {
		case sem <- struct{}{}:
		case <-ctx.Done():
			return
		}
		res, ok := checkTarget(ctx, c)
		<-sem
		if !ok {
			return // interrupted by shutdown
		}
		rec.record(c.t.ID, res, time.Now())

		elapsed := time.Since(started)
		next := c.t.Interval - elapsed
		if next <= 0 {
			log.Printf("[OVERRUN] %s took %v including any wait for a free slot, longer than its %v interval; checking again now",
				c.t.ID, elapsed.Truncate(time.Millisecond), c.t.Interval)
			next = 0
		}
		timer.Reset(next)
	}
}
