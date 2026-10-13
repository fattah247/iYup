package main

import (
	"io"
	"log"
	"math"
	"os"
	"path/filepath"
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

func testBase() baseDefaults {
	return baseDefaults{Interval: time.Second, Timeout: 2 * time.Second, Retries: 2, Method: "GET",
		UseRange: true, SLO: 0.999, MaxBodyBytes: 65536}
}

func intp(v int) *int           { return &v }
func floatp(v float64) *float64 { return &v }
func boolp(v bool) *bool        { return &v }

func writeFile(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "targets.yml")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestParseTargetsEnv(t *testing.T) {
	t.Parallel()
	targets := parseTargetsEnv(" https://a.example , ,https://b.example,https://a.example ")
	if want := []string{"https://a.example", "https://b.example"}; !reflect.DeepEqual(targets, want) {
		t.Fatalf("parseTargetsEnv() = %v, want %v", targets, want)
	}
}

func TestLoadConfigFromEnv(t *testing.T) {
	t.Setenv("PING_TARGET_URLS", "https://one.example,https://two.example")
	t.Setenv("PING_RETRY_COUNT", "0")
	t.Setenv("PING_HTTP_METHOD", "head")
	t.Setenv("PING_TARGETS_FILE", writeFile(t, "targets: [{url: https://ignored.example}]"))

	cfg, err := loadConfig()
	if err != nil {
		t.Fatalf("loadConfig() error: %v", err)
	}
	if cfg.Source != "PING_TARGET_URLS" || len(cfg.Targets) != 2 {
		t.Fatalf("PING_TARGET_URLS must win over the file: source=%s targets=%d", cfg.Source, len(cfg.Targets))
	}
	got := cfg.Targets[0]
	if got.ID != "https://one.example" || got.Retries != 0 || got.Method != "HEAD" || got.UseRange {
		t.Errorf("unexpected target %+v", got)
	}
}

func TestLoadConfigDefaultsWhenNothingConfigured(t *testing.T) {
	t.Setenv("PING_TARGET_URLS", "")
	t.Setenv("PING_TARGETS_FILE", "")
	cfg, err := loadConfig()
	if err != nil || len(cfg.Targets) != 2 || cfg.Targets[0].ID != "https://google.com" {
		t.Fatalf("got %+v, %v", cfg.Targets, err)
	}
}

func TestLoadConfigRejectsBadEnvTargets(t *testing.T) {
	t.Setenv("PING_TARGET_URLS", "https://ok.example,google.com,ftp://files.example")
	_, err := loadConfig()
	if err == nil {
		t.Fatal("invalid targets accepted")
	}
	for _, bad := range []string{"google.com", "ftp://files.example"} {
		if !strings.Contains(err.Error(), bad) {
			t.Errorf("error %q does not name %q", err, bad)
		}
	}
}

func TestTargetsFileMergesDefaults(t *testing.T) {
	t.Setenv("API_TOKEN", "s3cret")
	path := writeFile(t, `
defaults:
  interval: 45s
  retries: 1
  slo: 99.5
  headers: {X-Team: platform}
targets:
  - name: API
    url: https://api.example.com/health?key=${API_TOKEN}
    headers: {Authorization: "Bearer ${API_TOKEN}"}
    expect: {status: ["200", "204"], body_contains: '"ok"'}
    degraded_after: 800ms
  - url: https://www.example.com
    interval: 10s
    slo: 99.99
  - type: tcp
    address: db.example.com:5432
  - type: tcp
    address: example.com:443
    tls: true
  - type: dns
    host: Example.COM.
    record: mx
    expect: {answers: [mx1.example.com.]}
domains: [example.com, " Example.org. "]
`)
	targets, domains, err := loadTargetsFile(path, testBase())
	if err != nil {
		t.Fatal(err)
	}
	if len(targets) != 5 {
		t.Fatalf("got %d targets", len(targets))
	}
	api := targets[0]
	if api.ID != "https://api.example.com/health?key=${API_TOKEN}" {
		t.Errorf("ID must keep the placeholder so secrets never reach a label, got %q", api.ID)
	}
	if api.requestURL != "https://api.example.com/health?key=s3cret" || api.Headers.Get("Authorization") != "Bearer s3cret" {
		t.Errorf("secrets not expanded: url=%q auth=%q", api.requestURL, api.Headers.Get("Authorization"))
	}
	if api.Headers.Get("X-Team") != "platform" || api.Name != "API" || api.Interval != 45*time.Second || api.Retries != 1 {
		t.Errorf("defaults not merged: %+v", api)
	}
	if math.Abs(api.SLO-0.995) > 1e-9 || api.DegradedAfter != 800*time.Millisecond || api.UseRange {
		t.Errorf("slo/degraded/range wrong: %+v", api)
	}
	if !statusExpected(204, api.ExpectStatus) || statusExpected(301, api.ExpectStatus) {
		t.Errorf("expected statuses wrong: %+v", api.ExpectStatus)
	}
	if www := targets[1]; www.Interval != 10*time.Second || math.Abs(www.SLO-0.9999) > 1e-9 || !www.UseRange {
		t.Errorf("per-target override lost: %+v", www)
	}
	if targets[2].ID != "tcp://db.example.com:5432" || targets[3].ID != "tls://example.com:443" || !targets[3].TLS {
		t.Errorf("tcp ids: %q %q", targets[2].ID, targets[3].ID)
	}
	if dns := targets[4]; dns.ID != "dns://example.com/MX" || !reflect.DeepEqual(dns.ExpectAnswers, []string{"mx1.example.com"}) {
		t.Errorf("dns target: %+v", dns)
	}
	if !reflect.DeepEqual(domains, []string{"example.com", "example.org"}) {
		t.Errorf("domains = %v", domains)
	}
}

func TestTargetsFileReportsEveryProblem(t *testing.T) {
	path := writeFile(t, `
targets:
  - url: example.com
  - url: https://ok.example
    headers: {Authorization: "Bearer ${IYUP_TEST_UNSET_TOKEN}"}
  - type: tcp
    address: no-port
  - type: dns
    host: example.com
    record: SRV
  - url: https://ok.example/x
    expect: {status: [99x]}
  - url: https://dup.example
  - url: https://dup.example
  - type: ftp
domains: ["https://not-a-domain"]
`)
	_, _, err := loadTargetsFile(path, testBase())
	if err == nil {
		t.Fatal("broken file accepted")
	}
	for _, want := range []string{"absolute http(s) URL", "IYUP_TEST_UNSET_TOKEN", "address", "record must be", "invalid status", "duplicate", "type must be", "not a domain"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error does not mention %q:\n%v", want, err)
		}
	}
}

func TestTargetsFileRejectsTypos(t *testing.T) {
	path := writeFile(t, "targets:\n  - url: https://ok.example\n    intervall: 10s\n")
	if _, _, err := loadTargetsFile(path, testBase()); err == nil || !strings.Contains(err.Error(), "intervall") {
		t.Fatalf("misspelled field accepted: %v", err)
	}
}

func TestParseStatusRanges(t *testing.T) {
	t.Parallel()
	ranges, err := parseStatusRanges([]string{"2xx", "301", "400-404"})
	if err != nil {
		t.Fatal(err)
	}
	for code, want := range map[int]bool{200: true, 299: true, 301: true, 302: false, 404: true, 405: false} {
		if statusExpected(code, ranges) != want {
			t.Errorf("statusExpected(%d) = %v, want %v", code, !want, want)
		}
	}
	for _, bad := range []string{"6xx", "abc", "500-400", "99"} {
		if _, err := parseStatusRanges([]string{bad}); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestStaleAfterCoversWorstCaseCheck(t *testing.T) {
	t.Parallel()
	tg := target{Interval: 30 * time.Second, Timeout: 10 * time.Second, Retries: 2}
	worst := 30*time.Second + 3*backoffBase
	if got := tg.worstCaseCheck(); got != worst {
		t.Errorf("worstCaseCheck() = %v, want %v", got, worst)
	}
	if got, want := tg.staleAfter(), 60*time.Second+worst; got != want {
		t.Errorf("staleAfter() = %v, want %v", got, want)
	}
	if got := backoffBefore(20); got != maxBackoff {
		t.Errorf("backoffBefore(20) = %v, want cap %v", got, maxBackoff)
	}
}
