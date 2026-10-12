package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptrace"
	"sync"
	"syscall"
	"time"
)

const userAgent = "iyup-ping-agent"

// Failure reasons are a closed set so they are safe to use as a metric label.
const (
	reasonOK                = "ok"
	reasonHTTP4xx           = "http_4xx"
	reasonHTTP5xx           = "http_5xx"
	reasonTimeout           = "timeout"
	reasonDNS               = "dns"
	reasonConnectionRefused = "connection_refused"
	reasonTLS               = "tls"
	reasonNetwork           = "network"
	reasonError             = "error"
)

var allReasons = []string{
	reasonOK, reasonHTTP4xx, reasonHTTP5xx, reasonTimeout, reasonDNS,
	reasonConnectionRefused, reasonTLS, reasonNetwork, reasonError,
}

type probeOptions struct {
	Method       string
	UseRange     bool
	RetryCount   int
	MaxBodyBytes int64
}

func (c config) probeOptions() probeOptions {
	return probeOptions{
		Method:       c.Method,
		UseRange:     c.UseRange,
		RetryCount:   c.RetryCount,
		MaxBodyBytes: c.MaxBodyBytes,
	}
}

// checkResult is the verdict of one check, after retries.
type checkResult struct {
	Up         bool
	Reason     string
	StatusCode int // 0 when no HTTP response was received
	Latency    time.Duration
	Timings    phaseTimings
	CertExpiry time.Time // zero when no certificate was seen
	Attempts   int
	Err        error
}

func (r checkResult) retryable() bool {
	return r.StatusCode == 0 || r.StatusCode >= http.StatusInternalServerError
}

// runCycle checks every target once using a bounded worker pool. Cycles have no
// deadline of their own: each attempt is bounded by the client timeout, so a
// slow target delays the cycle (and shows up as stale) instead of being cut off
// and misreported as down.
func runCycle(ctx context.Context, client *http.Client, targets []string, concurrency int, opts probeOptions, rec *recorder) {
	if len(targets) == 0 {
		return
	}
	concurrency = max(1, min(concurrency, len(targets)))

	jobs := make(chan string)
	var wg sync.WaitGroup
	for i := 0; i < concurrency; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for target := range jobs {
				if res, ok := checkTarget(ctx, client, target, opts); ok {
					rec.record(target, res, time.Now())
				}
			}
		}()
	}

sendLoop:
	for _, target := range targets {
		select {
		case <-ctx.Done():
			break sendLoop
		case jobs <- target:
		}
	}
	close(jobs)
	wg.Wait()
}

// checkTarget probes target, retrying transport errors and 5xx responses with
// exponential backoff. ok is false when ctx ended before a verdict was reached:
// an interrupted check says nothing about the target and must not be recorded.
func checkTarget(ctx context.Context, client *http.Client, target string, opts probeOptions) (res checkResult, ok bool) {
	attempts := 1 + opts.RetryCount
	for attempt := 1; attempt <= attempts; attempt++ {
		if attempt > 1 {
			select {
			case <-ctx.Done():
				return checkResult{}, false
			case <-time.After(backoffBefore(attempt - 1)):
			}
		}
		res = attemptOnce(ctx, client, target, opts)
		res.Attempts = attempt
		if ctx.Err() != nil {
			return checkResult{}, false
		}
		if !res.retryable() {
			return res, true
		}
		if attempt < attempts {
			log.Printf("[RETRY] %s attempt=%d/%d reason=%s %s", target, attempt, attempts, res.Reason, describeFailure(res))
		}
	}
	return res, true
}

func attemptOnce(ctx context.Context, client *http.Client, target string, opts probeOptions) checkResult {
	timings := &traceTimings{}
	req, err := http.NewRequestWithContext(timings.attach(ctx), opts.Method, target, nil)
	if err != nil {
		return checkResult{Reason: reasonError, Err: err}
	}
	req.Header.Set("User-Agent", userAgent)
	sentRange := opts.UseRange && opts.Method == http.MethodGet
	if sentRange {
		req.Header.Set("Range", "bytes=0-0")
	}

	start := time.Now()
	resp, err := client.Do(req)
	latency := time.Since(start)
	if err != nil {
		res := checkResult{Reason: classifyError(err), Err: err, Latency: latency, Timings: timings.snapshot()}
		// A rejected certificate still tells us when it expires (or expired).
		var certErr *tls.CertificateVerificationError
		if errors.As(err, &certErr) && len(certErr.UnverifiedCertificates) > 0 {
			res.CertExpiry = certErr.UnverifiedCertificates[0].NotAfter
		}
		return res
	}
	if opts.MaxBodyBytes > 0 {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, opts.MaxBodyBytes))
	}
	_ = resp.Body.Close()

	res := checkResult{StatusCode: resp.StatusCode, Latency: latency, Timings: timings.snapshot()}
	res.Up, res.Reason = classifyStatus(resp.StatusCode, sentRange)
	if resp.TLS != nil && len(resp.TLS.PeerCertificates) > 0 {
		res.CertExpiry = resp.TLS.PeerCertificates[0].NotAfter
	}
	return res
}

func classifyStatus(code int, sentRange bool) (up bool, reason string) {
	switch {
	case code >= http.StatusInternalServerError:
		return false, reasonHTTP5xx
	case code == http.StatusRequestedRangeNotSatisfiable && sentRange:
		// The server answered; it just cannot serve byte 0 of an empty body.
		return true, reasonOK
	case code >= http.StatusBadRequest:
		return false, reasonHTTP4xx
	default:
		return true, reasonOK
	}
}

func classifyError(err error) string {
	var dnsErr *net.DNSError
	var certErr *tls.CertificateVerificationError
	var unknownAuthority x509.UnknownAuthorityError
	var hostnameErr x509.HostnameError
	var invalidCert x509.CertificateInvalidError
	var recordErr tls.RecordHeaderError
	var netErr net.Error
	var opErr *net.OpError

	switch {
	case errors.As(err, &dnsErr):
		return reasonDNS
	case errors.As(err, &certErr), errors.As(err, &unknownAuthority),
		errors.As(err, &hostnameErr), errors.As(err, &invalidCert), errors.As(err, &recordErr):
		return reasonTLS
	case errors.Is(err, syscall.ECONNREFUSED):
		return reasonConnectionRefused
	case errors.Is(err, context.DeadlineExceeded), errors.As(err, &netErr) && netErr.Timeout():
		return reasonTimeout
	case errors.As(err, &opErr):
		return reasonNetwork
	default:
		return reasonError
	}
}

func describeFailure(res checkResult) string {
	if res.Err != nil {
		return "err=" + res.Err.Error()
	}
	return "status=" + http.StatusText(res.StatusCode)
}

// phaseTimings are the connection phases of one attempt; zero means the phase
// did not complete.
type phaseTimings struct {
	DNS     time.Duration
	Connect time.Duration
	TLS     time.Duration
}

// traceTimings collects phaseTimings via httptrace. The hooks can fire from
// several goroutines (dual-stack dialing races IPv4 and IPv6), hence the lock.
type traceTimings struct {
	mu           sync.Mutex
	dnsStart     time.Time
	connectStart map[string]time.Time
	tlsStart     time.Time
	phases       phaseTimings
}

func (t *traceTimings) attach(ctx context.Context) context.Context {
	t.connectStart = map[string]time.Time{}
	return httptrace.WithClientTrace(ctx, &httptrace.ClientTrace{
		DNSStart: func(httptrace.DNSStartInfo) {
			t.mu.Lock()
			t.dnsStart = time.Now()
			t.mu.Unlock()
		},
		DNSDone: func(info httptrace.DNSDoneInfo) {
			t.mu.Lock()
			if info.Err == nil && !t.dnsStart.IsZero() {
				t.phases.DNS = time.Since(t.dnsStart)
			}
			t.mu.Unlock()
		},
		ConnectStart: func(network, addr string) {
			t.mu.Lock()
			t.connectStart[network+addr] = time.Now()
			t.mu.Unlock()
		},
		ConnectDone: func(network, addr string, err error) {
			t.mu.Lock()
			if start, ok := t.connectStart[network+addr]; ok && err == nil {
				t.phases.Connect = time.Since(start)
			}
			t.mu.Unlock()
		},
		TLSHandshakeStart: func() {
			t.mu.Lock()
			t.tlsStart = time.Now()
			t.mu.Unlock()
		},
		TLSHandshakeDone: func(_ tls.ConnectionState, err error) {
			t.mu.Lock()
			if err == nil && !t.tlsStart.IsZero() {
				t.phases.TLS = time.Since(t.tlsStart)
			}
			t.mu.Unlock()
		},
	})
}

func (t *traceTimings) snapshot() phaseTimings {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.phases
}
