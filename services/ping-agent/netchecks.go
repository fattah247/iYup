package main

import (
	"context"
	"crypto/tls"
	"fmt"
	"net"
	"strings"
	"time"
)

// tcpAttempt checks that a TCP port accepts connections, optionally completing
// a TLS handshake, timing DNS, connect and TLS separately.
func tcpAttempt(ctx context.Context, t target) checkResult {
	ctx, cancel := context.WithTimeout(ctx, t.Timeout)
	defer cancel()
	start := time.Now()
	timings := &traceTimings{}

	// The dialer tries every address the name resolves to, splitting the
	// timeout between them, so a dual-stack name whose service listens on only
	// one family is reachable, not refused.
	var dialer net.Dialer
	conn, err := dialer.DialContext(timings.attach(ctx), "tcp", t.Address)
	if err != nil {
		return checkResult{Reason: classifyError(err), Err: err, Latency: time.Since(start), Timings: timings.snapshot()}
	}
	defer conn.Close()
	phases := timings.snapshot()

	res := checkResult{Up: true, Responded: true, Reason: reasonOK}
	if t.TLS {
		host, _, _ := net.SplitHostPort(t.Address)
		tlsStart := time.Now()
		tc := tls.Client(conn, &tls.Config{ServerName: host, MinVersion: tls.VersionTLS12})
		if err := tc.HandshakeContext(ctx); err != nil {
			res = checkResult{Reason: classifyError(err), Err: err, CertExpiry: rejectedCertExpiry(err)}
			if res.Reason != reasonTimeout {
				res.Reason = reasonTLS
			}
		} else {
			phases.TLS = time.Since(tlsStart)
			if certs := tc.ConnectionState().PeerCertificates; len(certs) > 0 {
				res.CertExpiry = certs[0].NotAfter
			}
		}
	}
	res.Latency = time.Since(start)
	res.Timings = phases
	return res
}

// dnsAttempt resolves a record and, when answers are expected, checks that
// each expected value is among them.
func dnsAttempt(ctx context.Context, t target) checkResult {
	ctx, cancel := context.WithTimeout(ctx, t.Timeout)
	defer cancel()
	resolver := net.DefaultResolver
	if t.Server != "" {
		server := t.Server
		resolver = &net.Resolver{
			PreferGo: true,
			Dial: func(ctx context.Context, network, _ string) (net.Conn, error) {
				var d net.Dialer
				return d.DialContext(ctx, network, server)
			},
		}
	}

	start := time.Now()
	answers, err := lookupRecord(ctx, resolver, t.Record, t.Host)
	latency := time.Since(start)
	if err != nil {
		reason := classifyError(err)
		if ctx.Err() == context.DeadlineExceeded {
			reason = reasonTimeout
		}
		return checkResult{Reason: reason, Err: err, Latency: latency}
	}
	res := checkResult{Responded: true, Latency: latency, Timings: phaseTimings{DNS: latency}}
	if len(answers) == 0 {
		res.Reason, res.Err = reasonDNS, fmt.Errorf("no %s records for %s", t.Record, t.Host)
		return res
	}
	have := map[string]bool{}
	for _, a := range answers {
		have[normalizeDNSName(a)] = true
	}
	for _, want := range t.ExpectAnswers {
		if !have[want] {
			res.Reason = reasonUnexpectedAnswer
			res.Err = fmt.Errorf("expected %s in %s answers, got %s", want, t.Record, strings.Join(answers, ", "))
			return res
		}
	}
	res.Up, res.Reason = true, reasonOK
	return res
}

func lookupRecord(ctx context.Context, r *net.Resolver, record, host string) ([]string, error) {
	switch record {
	case "A", "AAAA":
		addrs, err := r.LookupIPAddr(ctx, host)
		if err != nil {
			return nil, err
		}
		var out []string
		for _, a := range addrs {
			if (a.IP.To4() != nil) == (record == "A") {
				out = append(out, a.IP.String())
			}
		}
		return out, nil
	case "CNAME":
		cname, err := r.LookupCNAME(ctx, host)
		if err != nil {
			return nil, err
		}
		return []string{cname}, nil
	case "MX":
		mxs, err := r.LookupMX(ctx, host)
		if err != nil {
			return nil, err
		}
		var out []string
		for _, mx := range mxs {
			out = append(out, mx.Host)
		}
		return out, nil
	case "NS":
		nss, err := r.LookupNS(ctx, host)
		if err != nil {
			return nil, err
		}
		var out []string
		for _, ns := range nss {
			out = append(out, ns.Host)
		}
		return out, nil
	case "TXT":
		return r.LookupTXT(ctx, host)
	}
	return nil, fmt.Errorf("unsupported record type %s", record)
}
