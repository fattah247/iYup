package main

import (
	"io"
	"log"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

var testRegistry = prometheus.NewRegistry()

func TestMain(m *testing.M) {
	registerMetrics(testRegistry)
	backoffBase = time.Millisecond
	log.SetOutput(io.Discard)
	os.Exit(m.Run())
}

// series returns the value of name{labels} from the test registry and whether
// that exact series exists. Histograms report their sample count.
func series(t *testing.T, name string, labels map[string]string) (float64, bool) {
	t.Helper()
	families, err := testRegistry.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, mf := range families {
		if mf.GetName() != name {
			continue
		}
		for _, m := range mf.GetMetric() {
			if len(m.GetLabel()) != len(labels) {
				continue
			}
			match := true
			for _, lp := range m.GetLabel() {
				if labels[lp.GetName()] != lp.GetValue() {
					match = false
					break
				}
			}
			if !match {
				continue
			}
			switch {
			case m.Gauge != nil:
				return m.GetGauge().GetValue(), true
			case m.Counter != nil:
				return m.GetCounter().GetValue(), true
			case m.Histogram != nil:
				return float64(m.GetHistogram().GetSampleCount()), true
			}
		}
	}
	return 0, false
}

func TestParseTargetsEnv(t *testing.T) {
	t.Parallel()

	targets := parseTargetsEnv(" https://a.example , ,https://b.example,https://a.example ")
	want := []string{"https://a.example", "https://b.example"}
	if !reflect.DeepEqual(targets, want) {
		t.Fatalf("parseTargetsEnv() = %v, want %v", targets, want)
	}
}

func TestDefaultTargets(t *testing.T) {
	t.Parallel()

	want := []string{"https://google.com", "https://github.com"}
	if got := defaultTargets(); !reflect.DeepEqual(got, want) {
		t.Fatalf("defaultTargets() = %v, want %v", got, want)
	}
}

func TestValidateTargets(t *testing.T) {
	t.Parallel()

	if err := validateTargets([]string{"https://ok.example", "http://127.0.0.1:9/health"}); err != nil {
		t.Fatalf("valid targets rejected: %v", err)
	}
	err := validateTargets([]string{"https://ok.example", "google.com", "ftp://files.example", "https://"})
	if err == nil {
		t.Fatal("invalid targets accepted")
	}
	for _, bad := range []string{"google.com", "ftp://files.example", "https://"} {
		if !strings.Contains(err.Error(), bad) {
			t.Errorf("error %q does not name %q", err, bad)
		}
	}
	if strings.Contains(err.Error(), "ok.example") {
		t.Errorf("error %q names a valid target", err)
	}
}

func TestLoadConfig(t *testing.T) {
	t.Setenv("PING_TARGET_URLS", "https://one.example,https://two.example")
	t.Setenv("PING_RETRY_COUNT", "0")
	t.Setenv("PING_HTTP_METHOD", "head")

	cfg, err := loadConfig()
	if err != nil {
		t.Fatalf("loadConfig() error: %v", err)
	}
	if want := []string{"https://one.example", "https://two.example"}; !reflect.DeepEqual(cfg.Targets, want) {
		t.Errorf("Targets = %v, want %v", cfg.Targets, want)
	}
	if cfg.RetryCount != 0 {
		t.Errorf("RetryCount = %d, want 0 (explicitly disabling retries must be possible)", cfg.RetryCount)
	}
	if cfg.Method != "HEAD" {
		t.Errorf("Method = %q, want HEAD", cfg.Method)
	}
}

func TestLoadConfigRejectsBadInput(t *testing.T) {
	t.Setenv("PING_TARGET_URLS", "example.com")
	if _, err := loadConfig(); err == nil {
		t.Error("schemeless target accepted")
	}

	t.Setenv("PING_TARGET_URLS", "https://example.com")
	t.Setenv("PING_HTTP_METHOD", "POST")
	if _, err := loadConfig(); err == nil {
		t.Error("POST accepted as a check method")
	}
}

func TestStaleAfterCoversWorstCaseCheck(t *testing.T) {
	t.Parallel()

	cfg := config{Interval: 30 * time.Second, Timeout: 10 * time.Second, RetryCount: 2}
	// 3 attempts x 10s + backoffs of 1x and 2x the base (0.5s and 1s in production).
	worst := 30*time.Second + 3*backoffBase
	if got := cfg.worstCaseCheck(); got != worst {
		t.Errorf("worstCaseCheck() = %v, want %v", got, worst)
	}
	if got, want := cfg.staleAfter(), 60*time.Second+worst; got != want {
		t.Errorf("staleAfter() = %v, want %v", got, want)
	}
	if got := backoffBefore(20); got != maxBackoff {
		t.Errorf("backoffBefore(20) = %v, want cap %v", got, maxBackoff)
	}
}
