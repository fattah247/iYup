"""Turn ping-agent /metrics expositions into per-target status.

The agents are the source of truth for "right now". Each agent is a region; a
target's state is decided by majority across the regions with fresh data, the
same rule the alerts use. Nothing here invents a verdict: a target with no
data, or with data older than the agent's own staleness budget, is unknown.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from prometheus_client.parser import text_string_to_metric_families

STATE_UP = "up"
STATE_DOWN = "down"
STATE_UNKNOWN = "unknown"
DEFAULT_REGION = "default"

_GAUGES = {
    "ping_up": "up",
    "ping_degraded": "degraded",
    "ping_last_latency_seconds": "last_latency",
    "ping_last_status_code": "status_code",
    "ping_last_check_timestamp_seconds": "last_check",
    "ping_state_change_timestamp_seconds": "state_change",
    "ping_ssl_cert_expiry_days": "cert_expiry_days",
    "ping_dns_duration_seconds": "dns",
    "ping_connect_duration_seconds": "connect",
    "ping_tls_duration_seconds": "tls",
    "ping_interval_seconds": "interval",
    "ping_stale_after_seconds": "stale_after",
    "ping_degraded_threshold_seconds": "degraded_threshold",
    "ping_slo_objective_ratio": "slo",
}
_COUNTERS = {
    "ping_success_total": "success",
    "ping_failure_total": "failures",
    "ping_retries_total": "retries",
}


class MetricsFormatError(ValueError):
    """The agent answered, but not with parseable Prometheus text."""


@dataclass
class TargetMetrics:
    up: float | None = None
    degraded: float | None = None
    success: float = 0.0
    failures: float = 0.0
    retries: float = 0.0
    last_latency: float | None = None
    status_code: float | None = None
    reason: str | None = None
    last_check: float | None = None
    state_change: float | None = None
    cert_expiry_days: float | None = None
    dns: float | None = None
    connect: float | None = None
    tls: float | None = None
    interval: float | None = None
    stale_after: float | None = None
    degraded_threshold: float | None = None
    slo: float | None = None
    buckets: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class AgentSnapshot:
    region: str = DEFAULT_REGION
    targets: dict[str, TargetMetrics] = field(default_factory=dict)
    configured: dict[str, dict[str, str]] = field(default_factory=dict)  # target -> {name, kind}
    # Agent-wide values from agents that predate per-target settings.
    interval_seconds: float | None = None
    stale_after_seconds: float | None = None

    def target_urls(self) -> list[str]:
        return list(self.configured) or list(self.targets)


def parse_agent_metrics(text: str) -> AgentSnapshot:
    snapshot = AgentSnapshot()
    try:
        families = list(text_string_to_metric_families(text))
    except ValueError as exc:
        raise MetricsFormatError(f"unparseable metrics: {exc}") from exc

    for family in families:
        for sample in family.samples:
            name, labels, value = sample.name, sample.labels, sample.value
            if labels.get("region"):
                snapshot.region = labels["region"]
            target = labels.get("target")
            if not target:
                if name == "ping_interval_seconds":
                    snapshot.interval_seconds = value
                elif name == "ping_stale_after_seconds":
                    snapshot.stale_after_seconds = value
                continue
            if name == "ping_target_info":
                snapshot.configured[target] = {"name": labels.get("name", ""), "kind": labels.get("kind", "")}
                continue

            metrics = snapshot.targets.setdefault(target, TargetMetrics())
            if name in _GAUGES:
                if math.isfinite(value):
                    setattr(metrics, _GAUGES[name], value)
            elif name in _COUNTERS:
                if value >= 0:
                    setattr(metrics, _COUNTERS[name], value)
            elif name == "ping_last_result" and value == 1:
                metrics.reason = labels.get("reason")
            elif name == "ping_latency_seconds_bucket" and "le" in labels:
                try:
                    bound = float(labels["le"])  # float() also accepts "+Inf"
                except ValueError:
                    continue
                metrics.buckets.append((bound, value))

    for metrics in snapshot.targets.values():
        metrics.buckets.sort(key=lambda bucket: bucket[0])
    return snapshot


def percentile_from_histogram(buckets: list[tuple[float, float]], quantile: float) -> float | None:
    """Estimate a quantile from sorted cumulative buckets [(le, count)] by
    linear interpolation, the same way PromQL's histogram_quantile does."""
    if not buckets:
        return None
    total = buckets[-1][1]
    if total == 0:
        return None
    threshold = total * quantile
    prev_le, prev_count = 0.0, 0.0
    for le, count in buckets:
        if le == math.inf:
            break
        if count >= threshold:
            in_bucket = count - prev_count
            if in_bucket == 0:
                return le
            return prev_le + (threshold - prev_count) / in_bucket * (le - prev_le)
        prev_le, prev_count = le, count
    finite = [le for le, _ in buckets if le != math.inf]
    return finite[-1] if finite else None


def merge_buckets(bucket_lists: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    totals: dict[float, float] = {}
    for buckets in bucket_lists:
        for le, count in buckets:
            totals[le] = totals.get(le, 0.0) + count
    return sorted(totals.items())


def infer_kind(url: str) -> str:
    scheme = urlsplit(url).scheme
    return {"tcp": "tcp", "tls": "tcp", "dns": "dns"}.get(scheme, "http")


def display_name(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    return f"{parts.netloc}{path}" if parts.netloc else url


def iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _ms(seconds: float | None, digits: int = 2) -> float | None:
    return round(seconds * 1000, digits) if seconds is not None else None


def availability_percent(success: float, failures: float) -> float | None:
    """Share of successful checks; None when there were no checks at all,
    because "no data" is not "0% available"."""
    total = success + failures
    return (success / total) * 100 if total > 0 else None


def region_status(url: str, snapshot: AgentSnapshot, now: float) -> dict[str, object] | None:
    """One agent's view of one target, or None if that agent does not know it."""
    metrics = snapshot.targets.get(url)
    if metrics is None and url not in snapshot.configured:
        return None
    m = metrics or TargetMetrics()
    stale_after = m.stale_after if m.stale_after is not None else snapshot.stale_after_seconds
    age = now - m.last_check if m.last_check is not None else None
    stale = age is not None and stale_after is not None and age > stale_after
    if m.up is None:
        state, reason = STATE_UNKNOWN, "pending"
    elif stale:
        state, reason = STATE_UNKNOWN, m.reason
    else:
        state, reason = (STATE_UP if m.up == 1 else STATE_DOWN), m.reason
    return {
        "region": snapshot.region,
        "state": state,
        "reason": reason,
        "slow": state == STATE_UP and m.degraded == 1,
        "stale": stale,
        "http_status": int(m.status_code) if m.status_code is not None else None,
        "latency_ms": _ms(m.last_latency, 1),
        "last_checked_at": iso(m.last_check),
        "last_checked_seconds_ago": round(age, 1) if age is not None else None,
        "_metrics": m,
    }


def target_status(url: str, snapshots: list[AgentSnapshot], now: float) -> dict[str, object]:
    regions = [r for r in (region_status(url, s, now) for s in snapshots) if r is not None]
    info = next((s.configured[url] for s in snapshots if url in s.configured), {})
    fresh = [r for r in regions if r["state"] != STATE_UNKNOWN]
    down = [r for r in fresh if r["state"] == STATE_DOWN]
    up = [r for r in fresh if r["state"] == STATE_UP]

    if not regions:
        state, reason, rep = STATE_UNKNOWN, "not_monitored", None
    elif not fresh:
        rep = regions[0]
        state, reason = STATE_UNKNOWN, rep["reason"]
    elif len(down) * 2 > len(fresh):
        rep = down[0]
        reasons = [r["reason"] for r in down]
        state, reason = STATE_DOWN, max(set(reasons), key=reasons.count)
    else:
        # The representative up region is the one with the median latency.
        rep = sorted(up, key=lambda r: r["latency_ms"] if r["latency_ms"] is not None else math.inf)[(len(up) - 1) // 2]
        state, reason = STATE_UP, rep["reason"]
    slow = state == STATE_UP and sum(1 for r in up if r["slow"]) * 2 > len(up)

    all_metrics: list[TargetMetrics] = [r["_metrics"] for r in regions]
    m: TargetMetrics = rep["_metrics"] if rep else TargetMetrics()
    success = sum(x.success for x in all_metrics)
    failures = sum(x.failures for x in all_metrics)
    buckets = merge_buckets([x.buckets for x in all_metrics])
    availability = availability_percent(success, failures)
    expiries = [x.cert_expiry_days for x in all_metrics if x.cert_expiry_days is not None]
    intervals = [x.interval for x in all_metrics if x.interval is not None]
    stale_afters = [x.stale_after for x in all_metrics if x.stale_after is not None]
    thresholds = [x.degraded_threshold for x in all_metrics if x.degraded_threshold is not None]
    slos = [x.slo for x in all_metrics if x.slo is not None]
    stale = bool(regions) and not fresh and any(r["stale"] for r in regions)

    return {
        "url": url,
        "name": info.get("name") or display_name(url),
        "kind": info.get("kind") or infer_kind(url),
        "state": state,
        "up": state == STATE_UP,
        "slow": slow,
        "reason": reason,
        "stale": stale,
        "http_status": rep["http_status"] if rep else None,
        "last_checked_at": iso(max((x.last_check for x in all_metrics if x.last_check is not None), default=None)),
        "last_checked_seconds_ago": min(
            (r["last_checked_seconds_ago"] for r in regions if r["last_checked_seconds_ago"] is not None), default=None
        ),
        "state_since": iso(m.state_change) if state != STATE_UNKNOWN else None,
        "latency_ms": rep["latency_ms"] if rep else None,
        "latency_percentiles_ms": {
            name: _ms(percentile_from_histogram(buckets, q), 1)
            for name, q in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99))
        },
        "connection_ms": {"dns": _ms(m.dns), "tcp": _ms(m.connect), "tls": _ms(m.tls)},
        "availability": round(availability, 2) if availability is not None else None,
        "total_checks": int(success + failures),
        "success": int(success),
        "failures": int(failures),
        "retries": int(sum(x.retries for x in all_metrics)),
        "ssl_cert_expiry_days": round(min(expiries), 1) if expiries else None,
        "interval_seconds": max(intervals) if intervals else None,
        "stale_after_seconds": max(stale_afters) if stale_afters else None,
        "slow_threshold_ms": _ms(max(thresholds), 0) if thresholds else None,
        "slo_objective": round(max(slos) * 100, 4) if slos else None,
        "regions_total": len(fresh),
        "regions_failing": len(down),
        "regions": [{k: v for k, v in r.items() if k != "_metrics"} for r in regions],
        "maintenance": None,
    }


def overall_status(rows: list[dict[str, object]]) -> str:
    """Worst known state wins, and we only vouch for what we can see.
    Targets in an active maintenance window are set aside."""
    considered = [r for r in rows if not (r.get("maintenance") or {}).get("active")]
    if rows and not considered:
        return "maintenance"
    states = [r["state"] for r in considered]
    down = states.count(STATE_DOWN)
    if down:
        return "outage" if down == len(states) else "degraded"
    if not states or STATE_UNKNOWN in states:
        return "unknown"
    if any(r.get("slow") for r in considered):
        return "degraded"
    return "operational"


def summarize(rows: list[dict[str, object]]) -> dict[str, int]:
    in_maintenance = [r for r in rows if (r.get("maintenance") or {}).get("active")]
    others = [r for r in rows if r not in in_maintenance]
    states = [r["state"] for r in others]
    return {
        "up": states.count(STATE_UP),
        "down": states.count(STATE_DOWN),
        "unknown": states.count(STATE_UNKNOWN),
        "slow": sum(1 for r in others if r.get("slow")),
        "maintenance": len(in_maintenance),
        "total": len(rows),
    }
