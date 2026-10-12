package main

import (
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// config is everything the agent reads from the environment, validated once at
// startup so a typo fails loudly instead of producing a target that is
// "down" forever.
type config struct {
	Targets      []string
	Interval     time.Duration
	Timeout      time.Duration
	Concurrency  int
	RetryCount   int
	MaxBodyBytes int64
	Method       string
	UseRange     bool
	Region       string
}

const maxBackoff = 5 * time.Second

// backoffBase is the pause before the first retry; it doubles per retry.
var backoffBase = 500 * time.Millisecond

func loadConfig() (config, error) {
	cfg := config{
		Targets:      parseTargetsEnv(os.Getenv("PING_TARGET_URLS")),
		Interval:     getenvDurationSeconds("PING_INTERVAL_SECONDS", 30),
		Timeout:      getenvDurationSeconds("PING_TIMEOUT_SECONDS", 10),
		Concurrency:  getenvInt("PING_CONCURRENCY", 5),
		RetryCount:   getenvNonNegativeInt("PING_RETRY_COUNT", 2),
		MaxBodyBytes: int64(getenvInt("PING_BODY_MAX_BYTES", 65536)),
		Method:       strings.ToUpper(getenvOrDefault("PING_HTTP_METHOD", http.MethodGet)),
		UseRange:     getenvBool("PING_RANGE_REQUEST", true),
		Region:       getenvOrDefault("PING_REGION", ""),
	}
	if len(cfg.Targets) == 0 {
		cfg.Targets = defaultTargets()
	}
	if err := validateTargets(cfg.Targets); err != nil {
		return config{}, err
	}
	if cfg.Method != http.MethodGet && cfg.Method != http.MethodHead {
		return config{}, fmt.Errorf("PING_HTTP_METHOD must be GET or HEAD, got %q", cfg.Method)
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

// worstCaseCheck is the longest a single target check can take: every attempt
// times out and every backoff is served in full.
func (c config) worstCaseCheck() time.Duration {
	total := time.Duration(1+c.RetryCount) * c.Timeout
	for n := 1; n <= c.RetryCount; n++ {
		total += backoffBefore(n)
	}
	return total
}

// staleAfter is how old a target's last check may get before consumers should
// stop trusting it: one missed cycle plus one worst-case check.
func (c config) staleAfter() time.Duration {
	return 2*c.Interval + c.worstCaseCheck()
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

func validateTargets(targets []string) error {
	var bad []string
	for _, target := range targets {
		u, err := url.Parse(target)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
			bad = append(bad, target)
		}
	}
	if len(bad) > 0 {
		return fmt.Errorf("PING_TARGET_URLS has invalid entries %q: each target must be an absolute http(s) URL such as https://example.com", bad)
	}
	return nil
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
