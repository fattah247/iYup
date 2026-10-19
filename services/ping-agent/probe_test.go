package main

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func mustTarget(t *testing.T, spec targetSpec) target {
	t.Helper()
	tg, err := resolveTarget(targetSpec{}, spec, testBase())
	if err != nil {
		t.Fatalf("resolveTarget(%+v): %v", spec, err)
	}
	return tg
}

func run(t *testing.T, spec targetSpec, transport http.RoundTripper) (checkResult, bool) {
	t.Helper()
	if transport == nil {
		transport = http.DefaultTransport
	}
	return checkTarget(context.Background(), newCheck(mustTarget(t, spec), transport))
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

func closedPort(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()
	return addr
}

func TestCheckUp(t *testing.T) {
	t.Parallel()
	srv, hits := statusSequence(t, http.StatusOK)
	res, ok := run(t, targetSpec{URL: srv.URL}, nil)
	if !ok || !res.Up || !res.Responded || res.Reason != reasonOK || res.StatusCode != 200 || res.Attempts != 1 || res.Degraded {
		t.Fatalf("got ok=%v %+v", ok, res)
	}
	if hits.Load() != 1 {
		t.Errorf("server hit %d times, want 1", hits.Load())
	}
}

func TestCheckRecoversOnRetry(t *testing.T) {
	t.Parallel()
	srv, _ := statusSequence(t, http.StatusServiceUnavailable, http.StatusOK)
	if res, ok := run(t, targetSpec{URL: srv.URL}, nil); !ok || !res.Up || res.Attempts != 2 {
		t.Fatalf("got ok=%v %+v, want up after 2 attempts", ok, res)
	}
}

func TestCheckPersistentServerError(t *testing.T) {
	t.Parallel()
	srv, hits := statusSequence(t, http.StatusBadGateway)
	res, ok := run(t, targetSpec{URL: srv.URL}, nil)
	if !ok || res.Up || res.Reason != reasonHTTP5xx || res.Attempts != 3 || hits.Load() != 3 {
		t.Fatalf("got ok=%v %+v hits=%d", ok, res, hits.Load())
	}
}

func TestClientErrorIsNotRetried(t *testing.T) {
	t.Parallel()
	srv, hits := statusSequence(t, http.StatusNotFound)
	res, _ := run(t, targetSpec{URL: srv.URL}, nil)
	if res.Up || res.Reason != reasonHTTP4xx || res.Attempts != 1 || hits.Load() != 1 {
		t.Fatalf("got %+v hits=%d", res, hits.Load())
	}
}

func TestExpectedStatus(t *testing.T) {
	t.Parallel()
	srv, hits := statusSequence(t, http.StatusOK)
	res, _ := run(t, targetSpec{URL: srv.URL, Expect: &expectSpec{Status: []string{"204"}}}, nil)
	if res.Up || res.Reason != reasonUnexpectedStatus || hits.Load() != 1 {
		t.Fatalf("200 when 204 is expected: %+v hits=%d", res, hits.Load())
	}
	created, _ := statusSequence(t, http.StatusCreated)
	if res, _ := run(t, targetSpec{URL: created.URL, Expect: &expectSpec{Status: []string{"2xx"}}}, nil); !res.Up {
		t.Fatalf("201 with 2xx expected: %+v", res)
	}
}

func TestClassifyStatusDefaults(t *testing.T) {
	t.Parallel()
	cases := []struct {
		code      int
		sentRange bool
		up        bool
		reason    string
	}{
		{200, true, true, reasonOK},
		{301, false, true, reasonOK},
		{416, true, true, reasonOK}, // empty body cannot satisfy bytes=0-0
		{416, false, false, reasonHTTP4xx},
		{503, true, false, reasonHTTP5xx},
	}
	for _, c := range cases {
		if up, reason := classifyStatus(c.code, c.sentRange); up != c.up || reason != c.reason {
			t.Errorf("classifyStatus(%d, %v) = %v/%s", c.code, c.sentRange, up, reason)
		}
	}
}

func TestBodyContains(t *testing.T) {
	t.Parallel()
	var sawRange atomic.Bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Range") != "" {
			sawRange.Store(true)
		}
		_, _ = w.Write([]byte(`{"status":"ok","db":"up"}`))
	}))
	defer srv.Close()

	if res, _ := run(t, targetSpec{URL: srv.URL, Expect: &expectSpec{BodyContains: `"db":"up"`}}, nil); !res.Up {
		t.Fatalf("matching body: %+v", res)
	}
	if sawRange.Load() {
		t.Error("a Range request was sent although the body is needed")
	}
	res, _ := run(t, targetSpec{URL: srv.URL, Expect: &expectSpec{BodyContains: "maintenance"}}, nil)
	if res.Up || res.Reason != reasonBodyMismatch || res.StatusCode != 200 || res.Attempts != 3 {
		t.Fatalf("200 with the wrong body must be down (and retried): %+v", res)
	}
}

func TestBodyThatStallsIsATimeout(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("partial"))
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer srv.Close()
	res, _ := run(t, targetSpec{URL: srv.URL, Timeout: "200ms", Retries: intp(0), Expect: &expectSpec{BodyContains: "complete"}}, nil)
	if res.Up || res.Reason != reasonTimeout || res.Err == nil || res.StatusCode != 200 {
		t.Fatalf("got %+v, want a timeout while reading the body", res)
	}
}

func TestDegradedWhenSlow(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(60 * time.Millisecond)
	}))
	defer srv.Close()
	if res, _ := run(t, targetSpec{URL: srv.URL, DegradedAfter: "20ms"}, nil); !res.Up || !res.Degraded {
		t.Fatalf("slow response not degraded: %+v", res)
	}
	if res, _ := run(t, targetSpec{URL: srv.URL, DegradedAfter: "5s"}, nil); !res.Up || res.Degraded {
		t.Fatalf("fast enough response marked degraded: %+v", res)
	}
}

func TestRedirectPolicy(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/old" {
			http.Redirect(w, r, "/new", http.StatusFound)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	if res, _ := run(t, targetSpec{URL: srv.URL + "/old"}, nil); !res.Up || res.StatusCode != 200 {
		t.Fatalf("redirect not followed by default: %+v", res)
	}
	res, _ := run(t, targetSpec{URL: srv.URL + "/old", FollowRedirects: boolp(false), Expect: &expectSpec{Status: []string{"200"}}}, nil)
	if res.Up || res.StatusCode != 302 || res.Reason != reasonUnexpectedStatus {
		t.Fatalf("redirect followed despite follow_redirects: false: %+v", res)
	}
}

func TestHeadersAndUserAgent(t *testing.T) {
	t.Setenv("IYUP_TEST_TOKEN", "abc123")
	var gotAuth, gotUA, gotRange atomic.Value
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth.Store(r.Header.Get("Authorization"))
		gotUA.Store(r.Header.Get("User-Agent"))
		gotRange.Store(r.Header.Get("Range"))
	}))
	defer srv.Close()
	saved := userAgent
	setUserAgent("lab-b")
	defer func() { userAgent = saved }()

	run(t, targetSpec{URL: srv.URL, Headers: map[string]string{"Authorization": "Bearer ${IYUP_TEST_TOKEN}"}}, nil)
	if gotAuth.Load() != "Bearer abc123" {
		t.Errorf("Authorization = %q", gotAuth.Load())
	}
	if ua, _ := gotUA.Load().(string); !strings.Contains(ua, "region=lab-b") {
		t.Errorf("User-Agent %q does not name the region", ua)
	}
	if gotRange.Load() != "bytes=0-0" {
		t.Errorf("Range = %q, want a one-byte request when nothing needs the body", gotRange.Load())
	}
}

func TestConnectionRefused(t *testing.T) {
	t.Parallel()
	res, ok := run(t, targetSpec{URL: "http://" + closedPort(t), Retries: intp(1)}, nil)
	if !ok || res.Up || res.Responded || res.Reason != reasonConnectionRefused || res.Attempts != 2 {
		t.Fatalf("got ok=%v %+v", ok, res)
	}
}

func TestTimeout(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(2 * time.Second):
		}
	}))
	defer srv.Close()
	if res, _ := run(t, targetSpec{URL: srv.URL, Timeout: "150ms", Retries: intp(0)}, nil); res.Up || res.Reason != reasonTimeout {
		t.Fatalf("got %+v", res)
	}
}

func TestUntrustedCertificateIsNotRetried(t *testing.T) {
	t.Parallel()
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {}))
	defer srv.Close()
	res, _ := run(t, targetSpec{URL: srv.URL}, http.DefaultTransport)
	if res.Up || res.Reason != reasonTLS || res.Attempts != 1 || res.CertExpiry.IsZero() {
		t.Fatalf("got %+v", res)
	}
}

func TestTrustedCertificateRecordsPhases(t *testing.T) {
	t.Parallel()
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {}))
	defer srv.Close()
	res, _ := run(t, targetSpec{URL: srv.URL}, srv.Client().Transport)
	if !res.Up || res.CertExpiry.IsZero() || res.Timings.Connect <= 0 || res.Timings.TLS <= 0 {
		t.Fatalf("got %+v", res)
	}
}

func TestTCPCheck(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer srv.Close()
	addr := strings.TrimPrefix(srv.URL, "http://")

	if res, _ := run(t, targetSpec{Type: "tcp", Address: addr}, nil); !res.Up || !res.Responded || res.Timings.Connect <= 0 {
		t.Fatalf("open port: %+v", res)
	}
	if res, _ := run(t, targetSpec{Type: "tcp", Address: closedPort(t), Retries: intp(0)}, nil); res.Up || res.Reason != reasonConnectionRefused {
		t.Fatalf("closed port: %+v", res)
	}
	_, port, _ := net.SplitHostPort(addr)
	byName := net.JoinHostPort("localhost", port)
	if res, _ := run(t, targetSpec{Type: "tcp", Address: byName}, nil); !res.Up || res.Timings.DNS <= 0 {
		t.Fatalf("by name (%s): %+v", byName, res)
	}
}

func TestTCPWithTLS(t *testing.T) {
	t.Parallel()
	srv := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer srv.Close()
	addr := strings.TrimPrefix(srv.URL, "https://")
	// The test certificate is self-signed, so the handshake is rejected; the
	// check must say "tls" and still read the certificate's expiry.
	res, _ := run(t, targetSpec{Type: "tcp", Address: addr, TLS: boolp(true)}, nil)
	if res.Up || res.Reason != reasonTLS || res.CertExpiry.IsZero() || res.Attempts != 1 {
		t.Fatalf("got %+v", res)
	}
}

func TestDNSCheck(t *testing.T) {
	t.Parallel()
	if res, _ := run(t, targetSpec{Type: "dns", Host: "localhost", Record: "A"}, nil); !res.Up || !res.Responded {
		t.Fatalf("localhost A: %+v", res)
	}
	res, _ := run(t, targetSpec{Type: "dns", Host: "localhost", Record: "A", Expect: &expectSpec{Answers: []string{"10.9.9.9"}}}, nil)
	if res.Up || res.Reason != reasonUnexpectedAnswer || res.Attempts != 1 {
		t.Fatalf("unexpected answer: %+v", res)
	}
	if res, _ := run(t, targetSpec{Type: "dns", Host: "does-not-exist.invalid", Retries: intp(0)}, nil); res.Up || res.Reason != reasonDNS {
		t.Fatalf("NXDOMAIN: %+v", res)
	}
}

// Regression: a check cut off by shutdown used to burn its remaining attempts
// instantly on the dead context and record a healthy target as down.
func TestInterruptedCheckIsNotAVerdict(t *testing.T) {
	t.Parallel()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(300 * time.Millisecond):
		}
	}))
	defer srv.Close()
	c := newCheck(mustTarget(t, targetSpec{URL: srv.URL}), http.DefaultTransport)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if res, ok := checkTarget(ctx, c); ok {
		t.Fatalf("interrupted check produced a verdict: %+v", res)
	}

	ctx2, cancel2 := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel2()
	runScheduler(ctx2, []*check{c}, 1, newRecorder())
	if _, exists := series(t, "ping_up", map[string]string{"target": srv.URL}); exists {
		t.Error("interrupted check set ping_up")
	}
}

func TestCancelStopsFurtherAttempts(t *testing.T) {
	t.Parallel()
	srv, hits := statusSequence(t, http.StatusServiceUnavailable)
	ctx, cancel := context.WithCancel(context.Background())
	transport := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		resp, err := http.DefaultTransport.RoundTrip(r)
		cancel()
		return resp, err
	})
	c := newCheck(mustTarget(t, targetSpec{URL: srv.URL, Retries: intp(5)}), transport)
	if _, ok := checkTarget(ctx, c); ok {
		t.Fatal("canceled check produced a verdict")
	}
	if hits.Load() != 1 {
		t.Errorf("server hit %d times after cancel, want 1", hits.Load())
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// Each target has its own loop: a target that hangs for the whole run holds
// one concurrency slot, and the others keep being checked with the rest.
func TestHungTargetDoesNotStarveOthers(t *testing.T) {
	t.Parallel()
	hung := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	defer hung.Close()
	fastA, _ := statusSequence(t, http.StatusOK)
	fastB, _ := statusSequence(t, http.StatusOK)

	checks := []*check{newCheck(mustTarget(t, targetSpec{URL: hung.URL, Timeout: "10s", Retries: intp(0)}), http.DefaultTransport)}
	for _, u := range []string{fastA.URL, fastB.URL} {
		checks = append(checks, newCheck(mustTarget(t, targetSpec{URL: u}), http.DefaultTransport))
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2600*time.Millisecond)
	defer cancel()
	runScheduler(ctx, checks, 2, newRecorder())
	for _, c := range checks[1:] {
		if v, ok := series(t, "ping_up", map[string]string{"target": c.t.ID}); !ok || v != 1 {
			t.Errorf("%s: ping_up=%v exists=%v, want 1", c.t.ID, v, ok)
		}
	}
	if _, ok := series(t, "ping_up", map[string]string{"target": hung.URL}); ok {
		t.Error("the hung check, cut off by shutdown, recorded a verdict")
	}
}

func TestRecorderTracksTransitionsReasonsAndSlowness(t *testing.T) {
	t.Parallel()
	const target = "https://recorder.example"
	rec := newRecorder()
	t0 := time.Unix(1_700_000_000, 0)
	lbl := map[string]string{"target": target}
	reason := func(r string) map[string]string { return map[string]string{"target": target, "reason": r} }

	rec.record(target, checkResult{Up: true, Degraded: true, Responded: true, Reason: reasonOK, StatusCode: 200, Latency: 40 * time.Millisecond, Attempts: 2}, t0)
	if v, _ := series(t, "ping_degraded", lbl); v != 1 {
		t.Errorf("degraded = %v, want 1", v)
	}
	if v, _ := series(t, "ping_retries_total", lbl); v != 1 {
		t.Errorf("retries = %v, want 1", v)
	}

	rec.record(target, checkResult{Up: true, Responded: true, Reason: reasonOK, StatusCode: 200, Latency: 50 * time.Millisecond, Attempts: 1}, t0.Add(30*time.Second))
	if v, _ := series(t, "ping_state_change_timestamp_seconds", lbl); v != float64(t0.Unix()) {
		t.Errorf("still up: state change moved to %v", v)
	}
	if v, _ := series(t, "ping_degraded", lbl); v != 0 {
		t.Errorf("degraded = %v after a fast check, want 0", v)
	}

	tDown := t0.Add(60 * time.Second)
	rec.record(target, checkResult{Reason: reasonConnectionRefused, Attempts: 3}, tDown)
	if v, _ := series(t, "ping_state_change_timestamp_seconds", lbl); v != float64(tDown.Unix()) {
		t.Errorf("went down: state change = %v", v)
	}
	if _, exists := series(t, "ping_last_result", reason(reasonOK)); exists {
		t.Error("stale ok reason series still exported")
	}
	if v, exists := series(t, "ping_last_result", reason(reasonConnectionRefused)); !exists || v != 1 {
		t.Errorf("current reason series = %v exists=%v", v, exists)
	}
	if _, exists := series(t, "ping_last_latency_seconds", lbl); exists {
		t.Error("latency still exported for a check with no response")
	}
	if v, _ := series(t, "ping_latency_seconds", lbl); v != 2 {
		t.Errorf("histogram has %v samples, want 2", v)
	}
}

func TestPublishConfigPerTarget(t *testing.T) {
	t.Parallel()
	tg := mustTarget(t, targetSpec{Name: "Info", URL: "https://info.example", Interval: "15s", DegradedAfter: "1s", SLO: floatp(99.95)})
	publishConfig([]target{tg})
	if v, ok := series(t, "ping_target_info", map[string]string{"target": tg.ID, "name": "Info", "kind": "http"}); !ok || v != 1 {
		t.Errorf("ping_target_info = %v exists=%v", v, ok)
	}
	lbl := map[string]string{"target": tg.ID}
	if v, _ := series(t, "ping_interval_seconds", lbl); v != 15 {
		t.Errorf("interval = %v", v)
	}
	if v, _ := series(t, "ping_slo_objective_ratio", lbl); v < 0.99949 || v > 0.99951 {
		t.Errorf("slo = %v", v)
	}
	if v, _ := series(t, "ping_degraded_threshold_seconds", lbl); v != 1 {
		t.Errorf("degraded threshold = %v", v)
	}
	for _, name := range []string{"ping_success_total", "ping_failure_total", "ping_retries_total"} {
		if v, ok := series(t, name, lbl); !ok || v != 0 {
			t.Errorf("%s = %v exists=%v, want 0 before the first check", name, v, ok)
		}
	}
	if _, ok := series(t, "ping_up", lbl); ok {
		t.Error("ping_up exported before any check")
	}
}

func TestDomainExpiryFromRDAP(t *testing.T) {
	t.Parallel()
	rdap := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/domain/renew-me.example":
			_, _ = w.Write([]byte(`{"events":[{"eventAction":"registration","eventDate":"2015-01-01T00:00:00Z"},
				{"eventAction":"expiration","eventDate":"2026-11-12T00:00:00Z"}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer rdap.Close()
	w := domainWatcher{base: rdap.URL + "/domain/", client: rdap.Client()}
	now := time.Date(2026, 10, 13, 0, 0, 0, 0, time.UTC)

	if !w.refresh(context.Background(), "renew-me.example", now) {
		t.Fatal("lookup reported failure")
	}
	if v, _ := series(t, "ping_domain_expiry_days", map[string]string{"domain": "renew-me.example"}); v != 30 {
		t.Errorf("expiry days = %v, want 30", v)
	}
	if w.refresh(context.Background(), "unknown.example", now) {
		t.Fatal("failed lookup reported success")
	}
	if v, _ := series(t, "ping_domain_expiry_errors_total", map[string]string{"domain": "unknown.example"}); v != 1 {
		t.Errorf("errors = %v, want 1", v)
	}
	if _, ok := series(t, "ping_domain_expiry_days", map[string]string{"domain": "unknown.example"}); ok {
		t.Error("a failed lookup exported an expiry")
	}
}

func TestDomainRetryBacksOff(t *testing.T) {
	t.Parallel()
	got := []time.Duration{domainRetryAfter(1), domainRetryAfter(2), domainRetryAfter(3), domainRetryAfter(20)}
	want := []time.Duration{5 * time.Minute, 10 * time.Minute, 20 * time.Minute, domainRefresh}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("domainRetryAfter(%d) = %v, want %v", i+1, got[i], want[i])
		}
	}
}
