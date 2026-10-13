package main

import (
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

// config is everything the agent reads at startup, validated once so a typo
// fails loudly instead of producing a target that is "down" forever.
type config struct {
	Targets     []target
	Domains     []string
	Source      string // where the targets came from, for the startup log
	Concurrency int
	Region      string
	RDAPBase    string
}

const maxBackoff = 5 * time.Second

// backoffBase is the pause before the first retry; it doubles per retry.
var backoffBase = 500 * time.Millisecond

// loadConfig reads targets from PING_TARGET_URLS when it is set (a quick
// override, and the original interface), otherwise from PING_TARGETS_FILE,
// otherwise it checks two public sites so a fresh checkout shows something.
func loadConfig() (config, error) {
	base := baseDefaults{
		Interval:     getenvDurationSeconds("PING_INTERVAL_SECONDS", 30),
		Timeout:      getenvDurationSeconds("PING_TIMEOUT_SECONDS", 10),
		Retries:      getenvNonNegativeInt("PING_RETRY_COUNT", 2),
		Method:       strings.ToUpper(getenvOrDefault("PING_HTTP_METHOD", http.MethodGet)),
		UseRange:     getenvBool("PING_RANGE_REQUEST", true),
		SLO:          getenvPercent("PING_SLO", 99.9) / 100,
		MaxBodyBytes: int64(getenvInt("PING_BODY_MAX_BYTES", 65536)),
	}
	cfg := config{
		Concurrency: getenvInt("PING_CONCURRENCY", 5),
		Region:      getenvOrDefault("PING_REGION", ""),
		RDAPBase:    getenvOrDefault("PING_RDAP_URL", "https://rdap.org/domain/"),
	}

	urls := parseTargetsEnv(os.Getenv("PING_TARGET_URLS"))
	file := strings.TrimSpace(os.Getenv("PING_TARGETS_FILE"))
	switch {
	case len(urls) > 0:
		cfg.Source = "PING_TARGET_URLS"
	case file != "":
		targets, domains, err := loadTargetsFile(file, base)
		if err != nil {
			return config{}, err
		}
		cfg.Targets, cfg.Domains, cfg.Source = targets, domains, file
		return cfg, nil
	default:
		urls = defaultTargets()
		cfg.Source = "built-in defaults"
	}

	var problems []string
	for _, u := range urls {
		t, err := resolveTarget(targetSpec{}, targetSpec{URL: u}, base)
		if err != nil {
			problems = append(problems, fmt.Sprintf("%q: %v", u, err))
			continue
		}
		cfg.Targets = append(cfg.Targets, t)
	}
	if len(problems) > 0 {
		return config{}, fmt.Errorf("PING_TARGET_URLS has invalid entries: %s", strings.Join(problems, "; "))
	}
	return cfg, nil
}

// backoffBefore returns the pause before retry number n (1-based).
func backoffBefore(n int) time.Duration {
	d := backoffBase << uint(n-1)
	if d <= 0 || d > maxBackoff {
		return maxBackoff
	}
	return d
}

// parseTargetsEnv splits a comma-separated list, trimming blanks and dropping
// duplicates (a duplicated URL would otherwise be checked and counted twice).
func parseTargetsEnv(value string) []string {
	var targets []string
	seen := map[string]bool{}
	for _, raw := range strings.Split(value, ",") {
		target := strings.TrimSpace(raw)
		if target == "" || seen[target] {
			continue
		}
		seen[target] = true
		targets = append(targets, target)
	}
	return targets
}

func defaultTargets() []string {
	return []string{"https://google.com", "https://github.com"}
}

func getenvDurationSeconds(name string, fallback int) time.Duration {
	return time.Duration(getenvInt(name, fallback)) * time.Second
}

// getenvInt reads a positive integer, falling back on blanks and bad values.
func getenvInt(name string, fallback int) int {
	value, ok := parseEnvInt(name)
	if !ok || value <= 0 {
		return fallback
	}
	return value
}

// getenvNonNegativeInt is getenvInt that also accepts 0, e.g. "no retries".
func getenvNonNegativeInt(name string, fallback int) int {
	value, ok := parseEnvInt(name)
	if !ok || value < 0 {
		return fallback
	}
	return value
}

func parseEnvInt(name string) (int, bool) {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return 0, false
	}
	value, err := strconv.Atoi(raw)
	return value, err == nil
}

func getenvPercent(name string, fallback float64) float64 {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil || value <= 0 || value >= 100 {
		return fallback
	}
	return value
}

func getenvOrDefault(name, fallback string) string {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	return value
}

func getenvBool(name string, fallback bool) bool {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	value, err := strconv.ParseBool(raw)
	if err != nil {
		return fallback
	}
	return value
}
