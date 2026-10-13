package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptrace"
	"strings"
	"sync"
	"syscall"
	"time"
)

// userAgent identifies the agent (and its region) to the sites it checks.
var userAgent = "iyup-ping-agent/2"

func setUserAgent(region string) {
	if region != "" {
		userAgent = "iyup-ping-agent/2 (region=" + region + ")"
	}
}

// Failure reasons are a closed set so they are safe to use as a metric label.
const (
	reasonOK                = "ok"
	reasonHTTP4xx           = "http_4xx"
	reasonHTTP5xx           = "http_5xx"
	reasonUnexpectedStatus  = "unexpected_status"
	reasonBodyMismatch      = "body_mismatch"
	reasonUnexpectedAnswer  = "unexpected_answer"
	reasonTimeout           = "timeout"
	reasonDNS               = "dns"
	reasonConnectionRefused = "connection_refused"
	reasonTLS               = "tls"
	reasonNetwork           = "network"
	reasonError             = "error"
)

var allReasons = []string{
	reasonOK, reasonHTTP4xx, reasonHTTP5xx, reasonUnexpectedStatus, reasonBodyMismatch,
	reasonUnexpectedAnswer, reasonTimeout, reasonDNS, reasonConnectionRefused, reasonTLS,
	reasonNetwork, reasonError,
}

// retryable reasons are the ones a second attempt might change. A 404, a
// rejected certificate or a wrong DNS answer will be just as wrong in a second.
var retryable = map[string]bool{
	reasonHTTP5xx: true, reasonBodyMismatch: true, reasonTimeout: true, reasonDNS: true,
	reasonConnectionRefused: true, reasonNetwork: true, reasonError: true,
}

// check is a target plus what it needs at runtime.
type check struct {
	t      target
	client *http.Client
}

func newCheck(t target, transport http.RoundTripper) *check {
	c := &check{t: t}
	if t.Kind == kindHTTP {
		c.client = &http.Client{Timeout: t.Timeout, Transport: transport}
		if !t.FollowRedirects {
			c.client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
		}
	}
	return c
}

// checkResult is the verdict of one check, after retries.
type checkResult struct {
	Up         bool
	Degraded   bool // up, but slower than the target's degraded_after
	Reason     string
	Responded  bool // the target answered (HTTP response, TCP connect, DNS answer)
	StatusCode int  // HTTP only; 0 otherwise
	Latency    time.Duration
	Timings    phaseTimings
	CertExpiry time.Time // zero when no certificate was seen
	Attempts   int
	Err        error
}

// checkTarget runs a check, retrying failures that a retry might fix, with
// exponential backoff. ok is false when ctx ended before a verdict: an
// interrupted check says nothing about the target and must not be recorded.
func checkTarget(ctx context.Context, c *check) (res checkResult, ok bool) {
	attempts := 1 + c.t.Retries
	for attempt := 1; attempt <= attempts; attempt++ {
		if attempt > 1 {
			select {
			case <-ctx.Done():
				return checkResult{}, false
			case <-time.After(backoffBefore(attempt - 1)):
			}
		}
		res = c.attempt(ctx)
		res.Attempts = attempt
		if ctx.Err() != nil {
			return checkResult{}, false
		}
		if res.Up || !retryable[res.Reason] {
			break
		}
		if attempt < attempts {
			log.Printf("[RETRY] %s attempt=%d/%d reason=%s %s", c.t.ID, attempt, attempts, res.Reason, describeFailure(res))
		}
	}
	res.Degraded = res.Up && c.t.DegradedAfter > 0 && res.Latency > c.t.DegradedAfter
	return res, true
}

func (c *check) attempt(ctx context.Context) checkResult {
	switch c.t.Kind {
	case kindTCP:
		return tcpAttempt(ctx, c.t)
	case kindDNS:
		return dnsAttempt(ctx, c.t)
	default:
		return httpAttempt(ctx, c.client, c.t)
	}
}

func httpAttempt(ctx context.Context, client *http.Client, t target) checkResult {
	timings := &traceTimings{}
	req, err := http.NewRequestWithContext(timings.attach(ctx), t.Method, t.requestURL, nil)
	if err != nil {
		return checkResult{Reason: reasonError, Err: err}
	}
	for name, values := range t.Headers {
		req.Header[name] = values
	}
	req.Header.Set("User-Agent", userAgent)
	if t.UseRange {
		req.Header.Set("Range", "bytes=0-0")
	}

	start := time.Now()
	resp, err := client.Do(req)
	latency := time.Since(start)
	if err != nil {
		res := checkResult{Reason: classifyError(err), Err: err, Latency: latency, Timings: timings.snapshot()}
		res.CertExpiry = rejectedCertExpiry(err)
		return res
	}
	var body []byte
	if t.BodyContains != "" {
		body, _ = io.ReadAll(io.LimitReader(resp.Body, t.MaxBodyBytes))
	} else if t.MaxBodyBytes > 0 {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, t.MaxBodyBytes))
	}
	_ = resp.Body.Close()

	res := checkResult{Responded: true, StatusCode: resp.StatusCode, Latency: latency, Timings: timings.snapshot()}
	if resp.TLS != nil && len(resp.TLS.PeerCertificates) > 0 {
		res.CertExpiry = resp.TLS.PeerCertificates[0].NotAfter
	}
	res.Up, res.Reason = evaluateStatus(t, resp.StatusCode)
	if res.Up && t.BodyContains != "" && !bytes.Contains(body, []byte(t.BodyContains)) {
		res.Up, res.Reason = false, reasonBodyMismatch
	}
	return res
}

// evaluateStatus applies the target's expected status codes; without any,
// 2xx and 3xx count as up.
func evaluateStatus(t target, code int) (bool, string) {
	if len(t.ExpectStatus) == 0 {
		return classifyStatus(code, t.UseRange)
	}
	switch {
	case statusExpected(code, t.ExpectStatus):
		return true, reasonOK
	case code >= http.StatusInternalServerError:
		return false, reasonHTTP5xx
	case code >= http.StatusBadRequest:
		return false, reasonHTTP4xx
	default:
		return false, reasonUnexpectedStatus
	}
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

// rejectedCertExpiry reads the expiry of a certificate the client refused to
// trust, so an expired certificate still reports how long ago it expired.
func rejectedCertExpiry(err error) time.Time {
	var certErr *tls.CertificateVerificationError
	if errors.As(err, &certErr) && len(certErr.UnverifiedCertificates) > 0 {
		return certErr.UnverifiedCertificates[0].NotAfter
	}
	return time.Time{}
}

func describeFailure(res checkResult) string {
	if res.Err != nil {
		return "err=" + res.Err.Error()
	}
	if res.StatusCode > 0 {
		return "status=" + strings.TrimSpace(http.StatusText(res.StatusCode))
	}
	return ""
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
