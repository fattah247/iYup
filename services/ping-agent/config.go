package main

import (
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

// config is everything the agent reads at startup. Targets are validated once,
// so a typo fails loudly instead of producing a target that is "down" forever;
// tuning variables that are unset or malformed fall back to their defaults.
type config struct {
	Targets     []target
	Domains     []string
	Source      string // where the targets came from, for the startup log
	Concurrency int
	Region      string
	RDAPBase    string
}

// loadConfig reads targets from PING_TARGET_URLS when it is set (a quick
// override, and the original interface), otherwise from PING_TARGETS_FILE,
// otherwise it checks two public sites so a fresh checkout shows something.
func loadConfig() (config, error) {
	base := baseDefaults{
		Interval:     time.Duration(getenvInt("PING_INTERVAL_SECONDS", 30, 1)) * time.Second,
		Timeout:      time.Duration(getenvInt("PING_TIMEOUT_SECONDS", 10, 1)) * time.Second,
		Retries:      getenvInt("PING_RETRY_COUNT", 2, 0),
		Method:       getenvOrDefault("PING_HTTP_METHOD", http.MethodGet),
		UseRange:     getenvBool("PING_RANGE_REQUEST", true),
		SLO:          getenvPercent("PING_SLO", 99.9) / 100,
		MaxBodyBytes: int64(getenvInt("PING_BODY_MAX_BYTES", 65536, 1)),
	}
	cfg := config{
		Concurrency: getenvInt("PING_CONCURRENCY", 5, 1),
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
		urls = []string{"https://google.com", "https://github.com"}
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

// parseTargetsEnv splits a comma-separated list, trimming blanks and dropping
// duplicates (a duplicated URL would otherwise be checked and counted twice).
func parseTargetsEnv(value string) []string {
	var urls []string
	seen := map[string]bool{}
	for _, raw := range strings.Split(value, ",") {
		url := strings.TrimSpace(raw)
		if url == "" || seen[url] {
			continue
		}
		seen[url] = true
		urls = append(urls, url)
	}
	return urls
}

// getenvInt reads an integer of at least minimum, falling back on blanks and
// bad values.
func getenvInt(name string, fallback, minimum int) int {
	value, err := strconv.Atoi(strings.TrimSpace(os.Getenv(name)))
	if err != nil || value < minimum {
		return fallback
	}
	return value
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
