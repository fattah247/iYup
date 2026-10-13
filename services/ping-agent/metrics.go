package main

import (
	"log"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

var (
	pingSuccess = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ping_success_total",
			Help: "Total number of successful checks",
		},
		[]string{"target"},
	)
	pingFailure = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ping_failure_total",
			Help: "Total number of failed checks (counted once per check, after retries)",
		},
		[]string{"target"},
	)
	pingRetries = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "ping_retries_total",
			Help: "Total number of retry attempts; a target that stays up but keeps retrying is flaky",
		},
		[]string{"target"},
	)
	// Buckets are dense where web responses actually land (0.1-3s). With the
	// default buckets a target answering in 0.6-1.4s reported a p95 of 2.4s,
	// interpolated across the empty 1-2.5s bucket.
	pingLatency = prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "ping_latency_seconds",
			Help:    "Response latency of checks that received an HTTP response, in seconds",
			Buckets: []float64{0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1, 1.25, 1.5, 2, 3, 5, 10},
		},
		[]string{"target"},
	)
	pingUp = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ping_up",
			Help: "Current target status (1 = up, 0 = down)",
		},
		[]string{"target"},
	)
	pingLastLatency = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ping_last_latency_seconds",
			Help: "Latency of the most recent check; absent when it received no HTTP response",
		},
		[]string{"target"},
	)
	pingLastStatusCode = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ping_last_status_code",
			Help: "HTTP status code of the most recent check; absent when it received no HTTP response",
		},
		[]string{"target"},
	)
	pingLastResult = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ping_last_result",
			Help: "Outcome of the most recent check; exactly one reason series per target is present, with value 1",
		},
		[]string{"target", "reason"},
	)
	pingLastCheck = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ping_last_check_timestamp_seconds",
			Help: "Unix time the most recent check finished",
		},
		[]string{"target"},
	)
	pingStateChange = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ping_state_change_timestamp_seconds",
			Help: "Unix time the target last switched between up and down, or was first checked by this process",
		},
		[]string{"target"},
	)
	pingSSLCertExpiry = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ping_ssl_cert_expiry_days",
			Help: "Days until the target's TLS certificate expires (negative once expired)",
		},
		[]string{"target"},
	)
	pingDNSDuration = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ping_dns_duration_seconds",
			Help: "DNS resolution time of the most recent check; absent when the phase did not complete",
		},
		[]string{"target"},
	)
	pingConnectDuration = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ping_connect_duration_seconds",
			Help: "TCP connection time of the most recent check; absent when the phase did not complete",
		},
		[]string{"target"},
	)
	pingTLSDuration = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ping_tls_duration_seconds",
			Help: "TLS handshake time of the most recent check; absent when the phase did not complete",
		},
		[]string{"target"},
	)
	pingDegraded = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ping_degraded",
			Help: "1 when the latest check succeeded but took longer than the target's degraded_after",
		},
		[]string{"target"},
	)
	pingTargetInfo = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "ping_target_info",
			Help: "Configured targets, present (value 1) from startup even before the first check",
		},
		[]string{"target", "name", "kind"},
	)
	pingInterval = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "ping_interval_seconds",
		Help: "Configured pause between checks of the target",
	}, []string{"target"})
	pingStaleAfter = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "ping_stale_after_seconds",
		Help: "A check older than this should be treated as unknown, not up or down",
	}, []string{"target"})
	pingDegradedThreshold = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "ping_degraded_threshold_seconds",
		Help: "Latency above which a successful check counts as degraded; absent when not configured",
	}, []string{"target"})
	pingSLOObjective = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "ping_slo_objective_ratio",
		Help: "Availability objective for the target, e.g. 0.999",
	}, []string{"target"})
	pingDomainExpiry = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "ping_domain_expiry_days",
		Help: "Days until the domain registration expires, from RDAP",
	}, []string{"domain"})
	pingDomainChecked = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "ping_domain_expiry_last_check_timestamp_seconds",
		Help: "Unix time of the last successful RDAP lookup",
	}, []string{"domain"})
	pingDomainErrors = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "ping_domain_expiry_errors_total",
		Help: "Failed RDAP lookups",
	}, []string{"domain"})

	allMetrics = []prometheus.Collector{
		pingSuccess, pingFailure, pingRetries, pingLatency, pingUp, pingLastLatency,
		pingLastStatusCode, pingLastResult, pingLastCheck, pingStateChange,
		pingSSLCertExpiry, pingDNSDuration, pingConnectDuration, pingTLSDuration,
		pingDegraded, pingTargetInfo, pingInterval, pingStaleAfter, pingDegradedThreshold,
		pingSLOObjective, pingDomainExpiry, pingDomainChecked, pingDomainErrors,
	}
)

func registerMetrics(reg prometheus.Registerer) {
	for _, c := range allMetrics {
		reg.MustRegister(c)
	}
}

// publishConfig exposes each target and its settings before the first check
// finishes, so consumers can tell "not checked yet" from "down".
func publishConfig(targets []target) {
	for _, t := range targets {
		pingTargetInfo.WithLabelValues(t.ID, t.Name, t.Kind).Set(1)
		pingInterval.WithLabelValues(t.ID).Set(t.Interval.Seconds())
		pingStaleAfter.WithLabelValues(t.ID).Set(t.staleAfter().Seconds())
		pingSLOObjective.WithLabelValues(t.ID).Set(t.SLO)
		// Counters exist at zero from startup: a target that never failed has
		// ping_failure_total 0 rather than no series, so success + failure
		// arithmetic and increase() count from the very first check.
		pingSuccess.WithLabelValues(t.ID)
		pingFailure.WithLabelValues(t.ID)
		pingRetries.WithLabelValues(t.ID)
		if t.DegradedAfter > 0 {
			pingDegradedThreshold.WithLabelValues(t.ID).Set(t.DegradedAfter.Seconds())
		}
	}
}

// recorder turns check results into metrics and remembers the per-target state
// needed to detect up/down transitions.
type recorder struct {
	mu    sync.Mutex
	state map[string]targetState
}

type targetState struct {
	up     bool
	reason string
	since  time.Time
}

func newRecorder() *recorder {
	return &recorder{state: map[string]targetState{}}
}

func (r *recorder) record(target string, res checkResult, now time.Time) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if res.Responded {
		pingLatency.WithLabelValues(target).Observe(res.Latency.Seconds())
		pingLastLatency.WithLabelValues(target).Set(res.Latency.Seconds())
	} else {
		// No response means no response time; keeping the previous value would
		// make a dead target look fast.
		pingLastLatency.DeleteLabelValues(target)
	}
	if res.StatusCode > 0 {
		pingLastStatusCode.WithLabelValues(target).Set(float64(res.StatusCode))
	} else {
		pingLastStatusCode.DeleteLabelValues(target)
	}
	if res.Degraded {
		pingDegraded.WithLabelValues(target).Set(1)
	} else {
		pingDegraded.WithLabelValues(target).Set(0)
	}
	setOrDelete(pingDNSDuration, target, res.Timings.DNS)
	setOrDelete(pingConnectDuration, target, res.Timings.Connect)
	setOrDelete(pingTLSDuration, target, res.Timings.TLS)
	if !res.CertExpiry.IsZero() {
		pingSSLCertExpiry.WithLabelValues(target).Set(res.CertExpiry.Sub(now).Hours() / 24)
	}
	if res.Attempts > 1 {
		pingRetries.WithLabelValues(target).Add(float64(res.Attempts - 1))
	}

	prev, seen := r.state[target]
	if seen && prev.reason != res.Reason {
		pingLastResult.DeleteLabelValues(target, prev.reason)
	}
	pingLastResult.WithLabelValues(target, res.Reason).Set(1)

	if res.Up {
		pingSuccess.WithLabelValues(target).Inc()
		pingUp.WithLabelValues(target).Set(1)
	} else {
		pingFailure.WithLabelValues(target).Inc()
		pingUp.WithLabelValues(target).Set(0)
	}
	pingLastCheck.WithLabelValues(target).Set(unixSeconds(now))

	next := targetState{up: res.Up, reason: res.Reason, since: prev.since}
	if !seen || prev.up != res.Up {
		next.since = now
		pingStateChange.WithLabelValues(target).Set(unixSeconds(now))
	}
	r.state[target] = next

	logResult(target, res, prev, seen, now)
}

func logResult(target string, res checkResult, prev targetState, seen bool, now time.Time) {
	latency := res.Latency.Truncate(time.Millisecond)
	switch {
	case seen && prev.up && !res.Up:
		log.Printf("[OUTAGE] %s went down reason=%s attempts=%d %s", target, res.Reason, res.Attempts, describeFailure(res))
	case seen && !prev.up && res.Up:
		log.Printf("[RECOVERED] %s is back up after %v down, status=%d latency=%v",
			target, now.Sub(prev.since).Truncate(time.Second), res.StatusCode, latency)
	case res.Up && res.Degraded:
		log.Printf("[SLOW] %s latency=%v is above its degraded_after threshold, status=%d attempts=%d",
			target, latency, res.StatusCode, res.Attempts)
	case res.Up:
		log.Printf("[UP] %s status=%d latency=%v dns=%v tcp=%v tls=%v attempts=%d",
			target, res.StatusCode, latency,
			res.Timings.DNS.Truncate(time.Microsecond),
			res.Timings.Connect.Truncate(time.Microsecond),
			res.Timings.TLS.Truncate(time.Microsecond),
			res.Attempts)
	default:
		log.Printf("[DOWN] %s reason=%s attempts=%d %s", target, res.Reason, res.Attempts, describeFailure(res))
	}
}

func setOrDelete(vec *prometheus.GaugeVec, target string, d time.Duration) {
	if d > 0 {
		vec.WithLabelValues(target).Set(d.Seconds())
	} else {
		vec.DeleteLabelValues(target)
	}
}

func unixSeconds(t time.Time) float64 {
	return float64(t.UnixNano()) / 1e9
}
