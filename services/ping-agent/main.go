package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

func main() {
	// `ping-agent validate [targets.yml]` checks a configuration and exits,
	// so a typo fails CI instead of a deployment.
	if len(os.Args) > 1 && os.Args[1] == "validate" {
		os.Exit(validate(os.Args[2:]))
	}

	cfg, err := loadConfig()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// Deploy several agents with different PING_REGION values to compare
	// regions in a single Prometheus; the region becomes a constant label.
	var reg prometheus.Registerer = prometheus.DefaultRegisterer
	if cfg.Region != "" {
		reg = prometheus.WrapRegistererWith(prometheus.Labels{"region": cfg.Region}, reg)
	}
	registerMetrics(reg)
	publishConfig(cfg.Targets)
	setUserAgent(cfg.Region)

	log.Printf("checking %d target(s) from %s (concurrency=%d region=%q)",
		len(cfg.Targets), cfg.Source, cfg.Concurrency, cfg.Region)

	// Disable keep-alives so each check measures the full connection
	// lifecycle (DNS + TCP + TLS), matching what a first-time visitor sees.
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DisableKeepAlives = true

	checks := make([]*check, 0, len(cfg.Targets))
	for _, t := range cfg.Targets {
		log.Printf("  %s every %v (timeout=%v retries=%d slo=%g%%%s)",
			t.ID, t.Interval, t.Timeout, t.Retries, t.SLO*100, describeExtras(t))
		if worst := t.worstCaseCheck(); worst > t.Interval {
			log.Printf("  note: %s can take up to %v to check when unreachable, longer than its interval", t.ID, worst)
		}
		checks = append(checks, newCheck(t, transport))
	}

	go runScheduler(ctx, checks, cfg.Concurrency, newRecorder())
	go domainWatcher{base: cfg.RDAPBase, client: http.DefaultClient, domains: cfg.Domains}.run(ctx)

	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("ok"))
	})
	server := &http.Server{
		Addr:              ":8080",
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	serverErrCh := make(chan error, 1)
	go func() {
		serverErrCh <- server.ListenAndServe()
	}()
	log.Println("metrics available on :8080/metrics")

	var serverErr error
	select {
	case <-ctx.Done():
	case err := <-serverErrCh:
		serverErr = err
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		log.Printf("HTTP server shutdown error: %v", err)
	}

	if serverErr == nil {
		serverErr = <-serverErrCh
	}
	if serverErr != nil && !errors.Is(serverErr, http.ErrServerClosed) {
		log.Fatalf("HTTP server error: %v", serverErr)
	}
}

func validate(args []string) int {
	if len(args) > 0 {
		os.Setenv("PING_TARGETS_FILE", args[0])
		os.Unsetenv("PING_TARGET_URLS")
	}
	cfg, err := loadConfig()
	if err != nil {
		fmt.Fprintf(os.Stderr, "invalid: %v\n", err)
		return 1
	}
	fmt.Printf("ok: %d target(s) and %d domain(s) from %s\n", len(cfg.Targets), len(cfg.Domains), cfg.Source)
	for _, t := range cfg.Targets {
		fmt.Printf("  %-5s %s every %v%s\n", t.Kind, t.ID, t.Interval, describeExtras(t))
	}
	return 0
}

// describeExtras lists the non-default parts of a target for the startup log.
func describeExtras(t target) string {
	var parts []string
	if t.Name != "" {
		parts = append(parts, "name="+t.Name)
	}
	if len(t.ExpectStatus) > 0 {
		parts = append(parts, "expect_status=custom")
	}
	if t.BodyContains != "" {
		parts = append(parts, "body_contains=set")
	}
	if t.DegradedAfter > 0 {
		parts = append(parts, "degraded_after="+t.DegradedAfter.String())
	}
	if len(t.Headers) > 0 {
		parts = append(parts, fmt.Sprintf("headers=%d", len(t.Headers)))
	}
	if len(parts) == 0 {
		return ""
	}
	return " " + strings.Join(parts, " ")
}
