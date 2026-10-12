package main

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func opts(retries int) probeOptions {
	return probeOptions{Method: http.MethodGet, UseRange: true, RetryCount: retries, MaxBodyBytes: 1024}
}

// statusSequence serves the given status codes in order, repeating the last.
func statusSequence(t *testing.T, codes ...int) (*httptest.Server, *atomic.Int32) {
	t.Helper()
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		n := int(hits.Add(1)) - 1
		w.WriteHeader(codes[min(n, len(codes)-1)])
	}))
	t.Cleanup(srv.Close)
	return srv, &hits
}

func closedPortURL(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()
	return "http://" + addr
}

func TestCheckTargetUp(t *testing.T) {
	t.Parallel()
	srv, hits := statusSequence(t, http.StatusOK)

	res, ok := checkTarget(context.Background(), srv.Client(), srv.URL, opts(2))
	if !ok || !res.Up || res.Reason != reasonOK || res.StatusCode != 200 || res.Attempts != 1 {
		t.Fatalf("got ok=%v %+v, want an up result on the first attempt", ok, res)
	}
	if hits.Load() != 1 {
		t.Errorf("server hit %d times, want 1", hits.Load())
	}
}

func TestCheckTargetRecoversOnRetry(t *testing.T) {
	t.Parallel()
	srv, _ := statusSequence(t, http.StatusServiceUnavailable, http.StatusOK)

	res, ok := checkTarget(context.Background(), srv.Client(), srv.URL, opts(2))
	if !ok || !res.Up || res.Attempts != 2 {
		t.Fatalf("got ok=%v %+v, want up after 2 attempts", ok, res)
	}
}

func TestCheckTargetPersistentServerError(t *testing.T) {
	t.Parallel()
	srv, hits := statusSequence(t, http.StatusBadGateway)

	res, ok := checkTarget(context.Background(), srv.Client(), srv.URL, opts(2))
	if !ok || res.Up || res.Reason != reasonHTTP5xx || res.StatusCode != 502 || res.Attempts != 3 {
		t.Fatalf("got ok=%v %+v, want down/http_5xx after 3 attempts", ok, res)
	}
	if hits.Load() != 3 {
		t.Errorf("server hit %d times, want 3", hits.Load())
	}
}

func TestCheckTargetClientErrorIsNotRetried(t *testing.T) {
	t.Parallel()
	srv, hits := statusSequence(t, http.StatusNotFound)

	res, ok := checkTarget(context.Background(), srv.Client(), srv.URL, opts(2))
	if !ok || res.Up || res.Reason != reasonHTTP4xx || res.Attempts != 1 {
		t.Fatalf("got ok=%v %+v, want down/http_4xx with no retry", ok, res)
	}
	if hits.Load() != 1 {
		t.Errorf("server hit %d times, want 1", hits.Load())
	}
}

func TestClassifyStatus(t *testing.T) {
	t.Parallel()
	cases := []struct {
		code      int
		sentRange bool
		up        bool
		reason    string
	}{
		{200, true, true, reasonOK},
		{206, true, true, reasonOK},
		{301, false, true, reasonOK},
		{416, true, true, reasonOK}, // empty body cannot satisfy bytes=0-0
		{416, false, false, reasonHTTP4xx},
		{401, false, false, reasonHTTP4xx},
		{503, true, false, reasonHTTP5xx},
	}
	for _, c := range cases {
		up, reason := classifyStatus(c.code, c.sentRange)
		if up != c.up || reason != c.reason {
			t.Errorf("classifyStatus(%d, range=%v) = %v/%s, want %v/%s", c.code, c.sentRange, up, reason, c.up, c.reason)
		}
	}
}

func TestCheckTargetConnectionRefused(t *testing.T) {
	t.Parallel()
	res, ok := checkTarget(context.Background(), http.DefaultClient, closedPortURL(t), opts(1))
	if !ok || res.Up || res.Reason != reasonConnectionRefused || res.StatusCode != 0 || res.Attempts != 2 {
		t.Fatalf("got ok=%v %+v, want down/connection_refused after 2 attempts", ok, res)
	}
}

func TestCheckTargetTimeout(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(2 * time.Second):
		}
	}))
	defer srv.Close()
	client := &http.Client{Timeout: 50 * time.Millisecond}

	res, ok := checkTarget(context.Background(), client, srv.URL, opts(0))
	if !ok || res.Up || res.Reason != reasonTimeout {
		t.Fatalf("got ok=%v %+v, want down/timeout", ok, res)
	}
}

func TestCheckTargetUntrustedCertificate(t *testing.T) {
	t.Parallel()
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {}))
	defer srv.Close()

	// A default client does not trust the test server's self-signed certificate.
	res, ok := checkTarget(context.Background(), &http.Client{}, srv.URL, opts(0))
	if !ok || res.Up || res.Reason != reasonTLS {
		t.Fatalf("got ok=%v %+v, want down/tls", ok, res)
	}
	if res.CertExpiry.IsZero() {
		t.Error("rejected certificate's expiry was not captured")
	}
}

func TestCheckTargetTrustedCertificateRecordsPhases(t *testing.T) {
	t.Parallel()
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {}))
	defer srv.Close()

	res, ok := checkTarget(context.Background(), srv.Client(), srv.URL, opts(0))
	if !ok || !res.Up {
		t.Fatalf("got ok=%v %+v, want up", ok, res)
	}
	if res.CertExpiry.IsZero() || res.Timings.Connect <= 0 || res.Timings.TLS <= 0 {
		t.Errorf("missing TLS details: expiry=%v phases=%+v", res.CertExpiry, res.Timings)
	}
}

// Regression: a check cut off by cancellation (shutdown, or the old per-cycle
// deadline) used to burn its remaining attempts instantly on the dead context
// and record a healthy target as down.
func TestInterruptedCheckIsNotAVerdict(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(300 * time.Millisecond):
		}
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if res, ok := checkTarget(ctx, srv.Client(), srv.URL, opts(2)); ok {
		t.Fatalf("interrupted check produced a verdict: %+v", res)
	}

	rec := newRecorder()
	ctx2, cancel2 := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel2()
	runCycle(ctx2, srv.Client(), []string{srv.URL}, 1, opts(2), rec)
	if _, exists := series(t, "ping_failure_total", map[string]string{"target": srv.URL}); exists {
		t.Error("interrupted check was recorded as a failure")
	}
	if _, exists := series(t, "ping_up", map[string]string{"target": srv.URL}); exists {
		t.Error("interrupted check set ping_up")
	}
}

func TestCancelStopsFurtherAttempts(t *testing.T) {
	t.Parallel()
	srv, hits := statusSequence(t, http.StatusServiceUnavailable)
	ctx, cancel := context.WithCancel(context.Background())
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		resp, err := http.DefaultTransport.RoundTrip(r)
		cancel() // shut down as the first attempt returns
		return resp, err
	})}

	if _, ok := checkTarget(ctx, client, srv.URL, opts(5)); ok {
		t.Fatal("check canceled during backoff produced a verdict")
	}
	if hits.Load() != 1 {
		t.Errorf("server hit %d times after cancel, want 1", hits.Load())
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// Without a cycle deadline, a slow target delays the cycle but cannot cause
// the targets queued behind it to be skipped.
func TestSlowTargetDoesNotStarveOthers(t *testing.T) {
	t.Parallel()
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(150 * time.Millisecond)
	}))
	defer slow.Close()
	fastA, _ := statusSequence(t, http.StatusOK)
	fastB, _ := statusSequence(t, http.StatusOK)

	targets := []string{slow.URL, fastA.URL, fastB.URL}
	runCycle(context.Background(), http.DefaultClient, targets, 1, opts(0), newRecorder())
	for _, target := range targets {
		if v, ok := series(t, "ping_up", map[string]string{"target": target}); !ok || v != 1 {
			t.Errorf("%s: ping_up=%v exists=%v, want 1", target, v, ok)
		}
	}
}

func TestRecorderTracksTransitionsAndReasons(t *testing.T) {
	t.Parallel()
	const target = "https://recorder.example"
	rec := newRecorder()
	t0 := time.Unix(1_700_000_000, 0)
	lbl := map[string]string{"target": target}
	reason := func(r string) map[string]string { return map[string]string{"target": target, "reason": r} }

	rec.record(target, checkResult{Up: true, Reason: reasonOK, StatusCode: 200, Latency: 40 * time.Millisecond, Attempts: 2}, t0)
	if v, _ := series(t, "ping_state_change_timestamp_seconds", lbl); v != float64(t0.Unix()) {
		t.Errorf("first check: state change = %v, want %v", v, t0.Unix())
	}
	if v, _ := series(t, "ping_retries_total", lbl); v != 1 {
		t.Errorf("retries = %v, want 1", v)
	}

	rec.record(target, checkResult{Up: true, Reason: reasonOK, StatusCode: 200, Latency: 50 * time.Millisecond, Attempts: 1}, t0.Add(30*time.Second))
	if v, _ := series(t, "ping_state_change_timestamp_seconds", lbl); v != float64(t0.Unix()) {
		t.Errorf("still up: state change moved to %v", v)
	}

	tDown := t0.Add(60 * time.Second)
	rec.record(target, checkResult{Up: false, Reason: reasonConnectionRefused, Attempts: 3}, tDown)
	if v, _ := series(t, "ping_state_change_timestamp_seconds", lbl); v != float64(tDown.Unix()) {
		t.Errorf("went down: state change = %v, want %v", v, tDown.Unix())
	}
	if _, exists := series(t, "ping_last_result", reason(reasonOK)); exists {
		t.Error("stale ok reason series still exported")
	}
	if v, exists := series(t, "ping_last_result", reason(reasonConnectionRefused)); !exists || v != 1 {
		t.Errorf("current reason series = %v exists=%v, want 1", v, exists)
	}
	if _, exists := series(t, "ping_last_latency_seconds", lbl); exists {
		t.Error("latency still exported for a check with no response")
	}
	if v, _ := series(t, "ping_latency_seconds", lbl); v != 2 {
		t.Errorf("histogram has %v samples, want 2 (failed connection must not count)", v)
	}
	if v, _ := series(t, "ping_last_check_timestamp_seconds", lbl); v != float64(tDown.Unix()) {
		t.Errorf("last check = %v, want %v", v, tDown.Unix())
	}
	if v, _ := series(t, "ping_retries_total", lbl); v != 3 {
		t.Errorf("retries = %v, want 3", v)
	}
}

func TestPublishConfigExposesTargetsBeforeFirstCheck(t *testing.T) {
	t.Parallel()
	cfg := config{Targets: []string{"https://info.example"}, Interval: 30 * time.Second, Timeout: 10 * time.Second}
	publishConfig(cfg)
	if v, ok := series(t, "ping_target_info", map[string]string{"target": "https://info.example"}); !ok || v != 1 {
		t.Errorf("ping_target_info = %v exists=%v, want 1", v, ok)
	}
	if _, ok := series(t, "ping_up", map[string]string{"target": "https://info.example"}); ok {
		t.Error("ping_up exported before any check")
	}
}
