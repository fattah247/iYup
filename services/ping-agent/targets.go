package main

import (
	"errors"
	"fmt"
	"maps"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	yaml "go.yaml.in/yaml/v2"
)

// Check kinds.
const (
	kindHTTP = "http"
	kindTCP  = "tcp"
	kindDNS  = "dns"
)

var dnsRecords = map[string]bool{"A": true, "AAAA": true, "CNAME": true, "MX": true, "TXT": true, "NS": true}

// target is one targets-file entry (or PING_TARGET_URLS URL) with defaults
// applied and validated. ID is the stable metric label: the URL as written for
// HTTP (placeholders unexpanded, so secrets never reach a label), tcp:// or
// tls:// plus the address, or dns://host/RECORD.
type target struct {
	ID   string
	Name string
	Kind string

	requestURL      string // the URL with ${VAR} expanded
	Method          string
	Headers         http.Header
	FollowRedirects bool
	UseRange        bool
	ExpectStatus    []statusRange // empty: 200-399 counts as up
	BodyContains    string

	Address string
	TLS     bool

	Host          string
	Record        string
	Server        string
	ExpectAnswers []string

	Interval      time.Duration
	Timeout       time.Duration
	Retries       int
	DegradedAfter time.Duration // 0 disables the slow state
	SLO           float64       // objective as a ratio, e.g. 0.999
	MaxBodyBytes  int64
}

// worstCaseCheck is the longest one check can take: every attempt times out
// and every backoff is served in full.
func (t target) worstCaseCheck() time.Duration {
	total := time.Duration(1+t.Retries) * t.Timeout
	for n := 1; n <= t.Retries; n++ {
		total += backoffBefore(n)
	}
	return total
}

// staleAfter is how old the last check may get before consumers should stop
// trusting it: one missed interval plus one worst-case check.
func (t target) staleAfter() time.Duration {
	return 2*t.Interval + t.worstCaseCheck()
}

type statusRange struct{ lo, hi int }

// parseStatusRanges accepts "200", "2xx" and "200-299".
func parseStatusRanges(values []string) ([]statusRange, error) {
	var ranges []statusRange
	for _, raw := range values {
		v := strings.ToLower(strings.TrimSpace(raw))
		switch {
		case len(v) == 3 && strings.HasSuffix(v, "xx") && v[0] >= '1' && v[0] <= '5':
			lo := int(v[0]-'0') * 100
			ranges = append(ranges, statusRange{lo, lo + 99})
		case strings.Contains(v, "-"):
			parts := strings.SplitN(v, "-", 2)
			lo, err1 := strconv.Atoi(strings.TrimSpace(parts[0]))
			hi, err2 := strconv.Atoi(strings.TrimSpace(parts[1]))
			if err1 != nil || err2 != nil || lo < 100 || hi > 599 || lo > hi {
				return nil, fmt.Errorf("invalid status range %q", raw)
			}
			ranges = append(ranges, statusRange{lo, hi})
		default:
			code, err := strconv.Atoi(v)
			if err != nil || code < 100 || code > 599 {
				return nil, fmt.Errorf("invalid status %q", raw)
			}
			ranges = append(ranges, statusRange{code, code})
		}
	}
	return ranges, nil
}

func statusExpected(code int, ranges []statusRange) bool {
	for _, r := range ranges {
		if code >= r.lo && code <= r.hi {
			return true
		}
	}
	return false
}

// File format. Durations are strings ("30s", "1m"); every field in defaults
// can be overridden per target.
type targetsFile struct {
	Defaults targetSpec   `yaml:"defaults"`
	Targets  []targetSpec `yaml:"targets"`
	Domains  []string     `yaml:"domains"`
}

type targetSpec struct {
	Name            string            `yaml:"name"`
	Type            string            `yaml:"type"`
	URL             string            `yaml:"url"`
	Address         string            `yaml:"address"`
	Host            string            `yaml:"host"`
	Record          string            `yaml:"record"`
	Server          string            `yaml:"server"`
	Method          string            `yaml:"method"`
	Headers         map[string]string `yaml:"headers"`
	Interval        string            `yaml:"interval"`
	Timeout         string            `yaml:"timeout"`
	DegradedAfter   string            `yaml:"degraded_after"`
	Retries         *int              `yaml:"retries"`
	SLO             *float64          `yaml:"slo"`
	TLS             *bool             `yaml:"tls"`
	FollowRedirects *bool             `yaml:"follow_redirects"`
	Expect          *expectSpec       `yaml:"expect"`
}

type expectSpec struct {
	Status       []string `yaml:"status"`
	BodyContains string   `yaml:"body_contains"`
	Answers      []string `yaml:"answers"`
}

// baseDefaults are the settings a target gets when neither the file's
// defaults nor the target itself say otherwise; they come from the environment.
type baseDefaults struct {
	Interval     time.Duration
	Timeout      time.Duration
	Retries      int
	Method       string
	UseRange     bool
	SLO          float64
	MaxBodyBytes int64
}

func loadTargetsFile(path string, base baseDefaults) ([]target, []string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, fmt.Errorf("reading %s: %w", path, err)
	}
	var file targetsFile
	if err := yaml.UnmarshalStrict(data, &file); err != nil {
		return nil, nil, fmt.Errorf("parsing %s: %w", path, err)
	}
	if len(file.Targets) == 0 {
		return nil, nil, fmt.Errorf("%s defines no targets", path)
	}
	var problems []string
	var targets []target
	seen := map[string]bool{}
	for i, spec := range file.Targets {
		t, err := resolveTarget(file.Defaults, spec, base)
		if err != nil {
			problems = append(problems, fmt.Sprintf("target %d (%s): %v", i+1, describeSpec(spec), err))
			continue
		}
		if seen[t.ID] {
			problems = append(problems, fmt.Sprintf("target %d: duplicate of %s", i+1, t.ID))
			continue
		}
		seen[t.ID] = true
		targets = append(targets, t)
	}
	var domains []string
	for _, d := range file.Domains {
		d = normalizeDNSName(d)
		if d == "" || strings.ContainsAny(d, "/: ") || !strings.Contains(d, ".") {
			problems = append(problems, fmt.Sprintf("domains: %q is not a domain name", d))
			continue
		}
		domains = append(domains, d)
	}
	if len(problems) > 0 {
		return nil, nil, fmt.Errorf("%s has problems:\n  %s", path, strings.Join(problems, "\n  "))
	}
	return targets, domains, nil
}

func describeSpec(s targetSpec) string {
	for _, v := range []string{s.Name, s.URL, s.Address, s.Host} {
		if v != "" {
			return v
		}
	}
	return "unnamed"
}

// resolveTarget merges defaults into spec and validates the result.
func resolveTarget(defaults, spec targetSpec, base baseDefaults) (target, error) {
	merged := mergeSpecs(defaults, spec)
	t := target{
		Name:            merged.Name,
		Kind:            strings.ToLower(firstNonEmpty(merged.Type, kindHTTP)),
		Method:          strings.ToUpper(firstNonEmpty(merged.Method, base.Method)),
		Retries:         base.Retries,
		SLO:             base.SLO,
		FollowRedirects: true,
		UseRange:        base.UseRange,
		MaxBodyBytes:    base.MaxBodyBytes,
	}
	var err error
	if t.Interval, err = durationOr(merged.Interval, base.Interval, "interval"); err != nil {
		return target{}, err
	}
	if t.Timeout, err = durationOr(merged.Timeout, base.Timeout, "timeout"); err != nil {
		return target{}, err
	}
	if t.DegradedAfter, err = durationOr(merged.DegradedAfter, 0, "degraded_after"); err != nil {
		return target{}, err
	}
	if t.Interval < time.Second {
		return target{}, errors.New("interval must be at least 1s")
	}
	if t.Timeout < 100*time.Millisecond {
		return target{}, errors.New("timeout must be at least 100ms")
	}
	if merged.Retries != nil {
		if *merged.Retries < 0 || *merged.Retries > 10 {
			return target{}, errors.New("retries must be between 0 and 10")
		}
		t.Retries = *merged.Retries
	}
	if merged.SLO != nil {
		if *merged.SLO <= 0 || *merged.SLO >= 100 {
			return target{}, errors.New("slo must be a percentage between 0 and 100, e.g. 99.9")
		}
		t.SLO = *merged.SLO / 100
	}
	if merged.FollowRedirects != nil {
		t.FollowRedirects = *merged.FollowRedirects
	}
	if merged.TLS != nil {
		t.TLS = *merged.TLS
	}
	if merged.Expect != nil {
		if t.ExpectStatus, err = parseStatusRanges(merged.Expect.Status); err != nil {
			return target{}, err
		}
		t.BodyContains = merged.Expect.BodyContains
		for _, a := range merged.Expect.Answers {
			t.ExpectAnswers = append(t.ExpectAnswers, normalizeDNSName(a))
		}
	}

	switch t.Kind {
	case kindHTTP:
		err = t.resolveHTTP(merged)
	case kindTCP:
		err = t.resolveTCP(merged)
	case kindDNS:
		err = t.resolveDNS(merged)
	default:
		err = fmt.Errorf("type must be http, tcp or dns, got %q", merged.Type)
	}
	if err != nil {
		return target{}, err
	}
	if t.Kind != kindHTTP && (len(t.ExpectStatus) > 0 || t.BodyContains != "") {
		return target{}, errors.New("expect.status and expect.body_contains only apply to http targets")
	}
	if t.Kind != kindDNS && len(t.ExpectAnswers) > 0 {
		return target{}, errors.New("expect.answers only applies to dns targets")
	}
	return t, nil
}

func (t *target) resolveHTTP(s targetSpec) error {
	if s.URL == "" {
		return errors.New("http targets need a url")
	}
	expanded, err := expandEnv(s.URL)
	if err != nil {
		return fmt.Errorf("url: %w", err)
	}
	u, err := url.Parse(expanded)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return errors.New("url must be an absolute http(s) URL such as https://example.com")
	}
	if t.Method != http.MethodGet && t.Method != http.MethodHead {
		return fmt.Errorf("method must be GET or HEAD, got %q", t.Method)
	}
	if t.Method == http.MethodHead && t.BodyContains != "" {
		return errors.New("expect.body_contains needs method GET")
	}
	t.ID = s.URL
	t.requestURL = expanded
	t.Headers = http.Header{}
	for name, value := range s.Headers {
		v, err := expandEnv(value)
		if err != nil {
			return fmt.Errorf("header %s: %w", name, err)
		}
		t.Headers.Set(name, v)
	}
	// A body assertion needs the body; a custom expectation changes what a
	// Range request's 416 would mean. Ask for one byte only when neither applies.
	if t.BodyContains != "" || len(t.ExpectStatus) > 0 || t.Method != http.MethodGet {
		t.UseRange = false
	}
	return nil
}

func (t *target) resolveTCP(s targetSpec) error {
	host, port, err := net.SplitHostPort(s.Address)
	if err != nil || host == "" {
		return errors.New("tcp targets need an address such as db.example.com:5432")
	}
	if n, err := strconv.Atoi(port); err != nil || n < 1 || n > 65535 {
		return fmt.Errorf("invalid port %q", port)
	}
	t.Address = s.Address
	t.ID = "tcp://" + s.Address
	if t.TLS {
		t.ID = "tls://" + s.Address
	}
	return nil
}

func (t *target) resolveDNS(s targetSpec) error {
	if s.Host == "" {
		return errors.New("dns targets need a host")
	}
	t.Host = normalizeDNSName(s.Host)
	t.Record = strings.ToUpper(firstNonEmpty(s.Record, "A"))
	if !dnsRecords[t.Record] {
		return fmt.Errorf("record must be one of A, AAAA, CNAME, MX, TXT, NS, got %q", s.Record)
	}
	t.ID = "dns://" + t.Host + "/" + t.Record
	if s.Server != "" {
		if _, _, err := net.SplitHostPort(s.Server); err != nil {
			return errors.New("server must be host:port, e.g. 1.1.1.1:53")
		}
		t.Server = s.Server
		t.ID += "@" + s.Server
	}
	return nil
}

func mergeSpecs(d, s targetSpec) targetSpec {
	m := s
	m.Type = firstNonEmpty(s.Type, d.Type)
	m.Method = firstNonEmpty(s.Method, d.Method)
	m.Interval = firstNonEmpty(s.Interval, d.Interval)
	m.Timeout = firstNonEmpty(s.Timeout, d.Timeout)
	m.DegradedAfter = firstNonEmpty(s.DegradedAfter, d.DegradedAfter)
	m.Record = firstNonEmpty(s.Record, d.Record)
	m.Server = firstNonEmpty(s.Server, d.Server)
	if m.Retries == nil {
		m.Retries = d.Retries
	}
	if m.SLO == nil {
		m.SLO = d.SLO
	}
	if m.TLS == nil {
		m.TLS = d.TLS
	}
	if m.FollowRedirects == nil {
		m.FollowRedirects = d.FollowRedirects
	}
	if m.Expect == nil {
		m.Expect = d.Expect
	}
	if len(d.Headers) > 0 {
		m.Headers = maps.Clone(d.Headers)
		maps.Copy(m.Headers, s.Headers)
	}
	return m
}

var envRef = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)\}`)

// expandEnv replaces ${VAR} with the variable's value. Only the braced form
// is recognised, so a literal "$" in a URL is left alone. A reference to an
// unset variable is an error: a silently empty token would just fail later.
func expandEnv(s string) (string, error) {
	var missing []string
	out := envRef.ReplaceAllStringFunc(s, func(ref string) string {
		name := envRef.FindStringSubmatch(ref)[1]
		v, ok := os.LookupEnv(name)
		if !ok {
			missing = append(missing, name)
		}
		return v
	})
	if len(missing) > 0 {
		return "", fmt.Errorf("references unset environment variable(s) %s", strings.Join(missing, ", "))
	}
	return out, nil
}

func durationOr(raw string, fallback time.Duration, field string) (time.Duration, error) {
	if strings.TrimSpace(raw) == "" {
		return fallback, nil
	}
	d, err := time.ParseDuration(strings.TrimSpace(raw))
	if err != nil || d < 0 {
		return 0, fmt.Errorf("%s: invalid duration %q (use e.g. 30s, 2m)", field, raw)
	}
	return d, nil
}

// normalizeDNSName lowercases a name and drops the root dot, so "Example.com."
// and "example.com" compare equal.
func normalizeDNSName(name string) string {
	return strings.TrimSuffix(strings.ToLower(strings.TrimSpace(name)), ".")
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}
