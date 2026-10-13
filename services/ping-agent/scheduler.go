package main

import (
	"context"
	"log"
	"sync"
	"time"
)

// runScheduler runs every check on its own interval until ctx ends. Each
// target has its own loop, so a slow target can delay only itself; a shared
// semaphore caps how many checks run at once. First checks are spread over
// up to two seconds so a restart does not fire everything simultaneously.
func runScheduler(ctx context.Context, checks []*check, concurrency int, rec *recorder) {
	if len(checks) == 0 {
		return
	}
	sem := make(chan struct{}, max(1, concurrency))
	var wg sync.WaitGroup
	for i, c := range checks {
		offset := 2 * time.Second * time.Duration(i) / time.Duration(len(checks))
		wg.Add(1)
		go func(c *check, offset time.Duration) {
			defer wg.Done()
			runLoop(ctx, c, sem, rec, offset)
		}(c, offset)
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

		next := c.t.Interval - time.Since(started)
		if next <= 0 {
			log.Printf("[SLOW] %s took %v, longer than its %v interval; checking again now",
				c.t.ID, time.Since(started).Truncate(time.Millisecond), c.t.Interval)
			next = 0
		}
		timer.Reset(next)
	}
}
