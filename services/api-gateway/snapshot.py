"""Turn the ping-agent's /metrics exposition into per-target status.

The agent is the source of truth for "right now": which targets exist, the
outcome of each target's latest check, and how fresh that outcome is. This
module never invents a verdict: a target with no data, or with data older than
the agent's own staleness budget, is reported as ``unknown`` rather than down.
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

_GAUGES = {
    "ping_up": "up",
    "ping_last_latency_seconds": "last_latency",
    "ping_last_status_code": "status_code",
    "ping_last_check_timestamp_seconds": "last_check",
    "ping_state_change_timestamp_seconds": "state_change",
    "ping_ssl_cert_expiry_days": "cert_expiry_days",
    "ping_dns_duration_seconds": "dns",
    "ping_connect_duration_seconds": "connect",
    "ping_tls_duration_seconds": "tls",
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
    buckets: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class AgentSnapshot:
    targets: dict[str, TargetMetrics] = field(default_factory=dict)
    configured: list[str] = field(default_factory=list)
    interval_seconds: float | None = None
    stale_after_seconds: float | None = None
    region: str | None = None

    def target_urls(self) -> list[str]:
        """Targets the agent checks; older agents without ping_target_info
        are described by whichever targets have data."""
        return self.configured or sorted(self.targets)


def parse_agent_metrics(text: str) -> AgentSnapshot:
    snapshot = AgentSnapshot()
    configured: set[str] = set()
    try:
        families = list(text_string_to_metric_families(text))
    except ValueError as exc:
        raise MetricsFormatError(f"unparseable metrics: {exc}") from exc

    for family in families:
        for sample in family.samples:
            name, labels, value = sample.name, sample.labels, sample.value
            if snapshot.region is None and labels.get("region"):
                snapshot.region = labels["region"]
            if name == "ping_interval_seconds":
                snapshot.interval_seconds = value
                continue
            if name == "ping_stale_after_seconds":
                snapshot.stale_after_seconds = value
                continue

            target = labels.get("target")
            if not target:
                continue
            if name == "ping_target_info":
                configured.add(target)
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
    snapshot.configured = sorted(configured)
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


def target_status(url: str, snapshot: AgentSnapshot, now: float) -> dict[str, object]:
    metrics = snapshot.targets.get(url)
    known_to_agent = url in snapshot.configured or metrics is not None
    m = metrics or TargetMetrics()

    age = now - m.last_check if m.last_check is not None else None
    stale = (
        age is not None
        and snapshot.stale_after_seconds is not None
        and age > snapshot.stale_after_seconds
    )
    if m.up is None:
        state = STATE_UNKNOWN
        reason = "pending" if known_to_agent else "not_monitored"
    elif stale:
        state = STATE_UNKNOWN
        reason = m.reason
    else:
        state = STATE_UP if m.up == 1 else STATE_DOWN
        reason = m.reason

    availability = availability_percent(m.success, m.failures)
    return {
        "url": url,
        "name": display_name(url),
        "state": state,
        "up": state == STATE_UP,
        "reason": reason,
        "stale": stale,
        "http_status": int(m.status_code) if m.status_code is not None else None,
        "last_checked_at": iso(m.last_check),
        "last_checked_seconds_ago": round(age, 1) if age is not None else None,
        "state_since": iso(m.state_change) if state != STATE_UNKNOWN else None,
        "latency_ms": _ms(m.last_latency, 1),
        "latency_percentiles_ms": {
            name: _ms(percentile_from_histogram(m.buckets, q), 1)
            for name, q in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99))
        },
        "connection_ms": {"dns": _ms(m.dns), "tcp": _ms(m.connect), "tls": _ms(m.tls)},
        "availability": round(availability, 2) if availability is not None else None,
        "total_checks": int(m.success + m.failures),
        "success": int(m.success),
        "failures": int(m.failures),
        "retries": int(m.retries),
        "ssl_cert_expiry_days": round(m.cert_expiry_days, 1) if m.cert_expiry_days is not None else None,
    }


def overall_status(states: list[str]) -> str:
    """Worst known state wins, and we only vouch for what we can see:
    any unknown target keeps the summary from claiming "operational"."""
    down = states.count(STATE_DOWN)
    if down:
        return "outage" if down == len(states) else "degraded"
    if not states or STATE_UNKNOWN in states:
        return "unknown"
    return "operational"
