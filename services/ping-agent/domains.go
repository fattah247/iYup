package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"
)

// Domain registrations lapse quietly and take the site, mail and certificates
// with them. RDAP (the JSON successor to WHOIS) publishes the expiry date;
// it changes rarely, so each domain is looked up twice a day.
const domainRefresh = 12 * time.Hour

type domainWatcher struct {
	base    string // RDAP lookup URL the domain is appended to
	client  *http.Client
	domains []string
}

func (w domainWatcher) run(ctx context.Context) {
	if len(w.domains) == 0 {
		return
	}
	// Each domain has its own schedule: twice a day after a good lookup, and
	// a quick retry with backoff after a failed one, so a network blip at
	// startup does not leave the expiry unknown for half a day.
	next := map[string]time.Time{}
	failures := map[string]int{}
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		now := time.Now()
		for _, d := range w.domains {
			if now.Before(next[d]) {
				continue
			}
			if w.refresh(ctx, d, now) {
				failures[d] = 0
				next[d] = now.Add(domainRefresh)
			} else {
				failures[d]++
				next[d] = now.Add(domainRetryAfter(failures[d]))
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// domainRetryAfter is the wait after the nth consecutive failed lookup.
func domainRetryAfter(n int) time.Duration {
	d := 5 * time.Minute << uint(n-1)
	if d <= 0 || d > domainRefresh {
		return domainRefresh
	}
	return d
}

func (w domainWatcher) refresh(ctx context.Context, domain string, now time.Time) bool {
	expiry, err := w.lookup(ctx, domain)
	if err != nil {
		if ctx.Err() == nil {
			pingDomainErrors.WithLabelValues(domain).Inc()
			log.Printf("[DOMAIN] %s expiry lookup failed: %v", domain, err)
		}
		return false
	}
	days := expiry.Sub(now).Hours() / 24
	pingDomainExpiry.WithLabelValues(domain).Set(days)
	pingDomainChecked.WithLabelValues(domain).Set(unixSeconds(now))
	log.Printf("[DOMAIN] %s registration expires %s (%.0f days)", domain, expiry.Format("2006-01-02"), days)
	return true
}

func (w domainWatcher) lookup(ctx context.Context, domain string) (time.Time, error) {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, w.base+domain, nil)
	if err != nil {
		return time.Time{}, err
	}
	req.Header.Set("Accept", "application/rdap+json, application/json")
	req.Header.Set("User-Agent", userAgent)
	resp, err := w.client.Do(req)
	if err != nil {
		return time.Time{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return time.Time{}, fmt.Errorf("RDAP answered HTTP %d", resp.StatusCode)
	}
	var payload struct {
		Events []struct {
			Action string `json:"eventAction"`
			Date   string `json:"eventDate"`
		} `json:"events"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&payload); err != nil {
		return time.Time{}, fmt.Errorf("unreadable RDAP response: %w", err)
	}
	for _, e := range payload.Events {
		if strings.EqualFold(e.Action, "expiration") {
			return time.Parse(time.RFC3339, e.Date)
		}
	}
	return time.Time{}, fmt.Errorf("RDAP response has no expiration event")
}
