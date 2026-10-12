package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

func main() {
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
	publishConfig(cfg)

	log.Printf("checking %d target(s) every %v (timeout=%v retries=%d concurrency=%d method=%s region=%q)",
		len(cfg.Targets), cfg.Interval, cfg.Timeout, cfg.RetryCount, cfg.Concurrency, cfg.Method, cfg.Region)
	if worst := cfg.worstCaseCheck(); worst > cfg.Interval {
		log.Printf("note: an unreachable target can take up to %v to check, longer than the %v interval; cycles will run back to back", worst, cfg.Interval)
	}

	// Disable keep-alives so each check measures the full connection
	// lifecycle (DNS + TCP + TLS), matching what a first-time visitor sees.
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DisableKeepAlives = true
	client := &http.Client{Timeout: cfg.Timeout, Transport: transport}

	go runScheduler(ctx, client, cfg, newRecorder())

	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
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

// runScheduler runs a cycle immediately and then on every tick. Cycles never
// overlap: if one outlasts the interval, the ticker's single buffered tick
// starts the next one right away and further ticks are dropped.
func runScheduler(ctx context.Context, client *http.Client, cfg config, rec *recorder) {
	ticker := time.NewTicker(cfg.Interval)
	defer ticker.Stop()

	opts := cfg.probeOptions()
	for {
		start := time.Now()
		runCycle(ctx, client, cfg.Targets, cfg.Concurrency, opts, rec)
		if took := time.Since(start); took > cfg.Interval && ctx.Err() == nil {
			log.Printf("[SLOW] check cycle took %v, longer than the %v interval", took.Truncate(time.Millisecond), cfg.Interval)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}
