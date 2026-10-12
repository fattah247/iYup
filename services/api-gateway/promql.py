"""Prometheus-backed views over time: windowed uptime, history buckets and
outages.

All history is derived from ``ping_up``, the agent's post-retry verdict, so a
blip that a retry absorbed never shows up as downtime here.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Iterable

import requests

DURATION_PATTERN = re.compile(r"^(\d+)([smhdw])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

# Prometheus refuses range queries returning more than 11,000 points per series.
MAX_POINTS = 10_000
MIN_STEP_SECONDS = 15  # the scrape interval; finer steps only repeat samples
# Missing data this short, with the target down on both sides, is an agent
# restart rather than a recovery: the same tolerance TargetDown uses.
BRIDGE_GAP_SECONDS = 120


class PrometheusError(Exception):
    """Prometheus could not answer; the message is safe to show callers."""


def parse_duration(text: str) -> int:
    """Parse a PromQL-style duration such as ``5m`` or ``7d`` into seconds."""
    match = DURATION_PATTERN.match(text)
    if not match:
        raise ValueError(
            f"Invalid window '{text}'. Use a number followed by s/m/h/d/w (e.g. 5m, 1h, 24h, 7d)."
        )
    seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
    if seconds <= 0:
        raise ValueError(f"Window '{text}' must be longer than zero.")
    return seconds


class TTLCache:
    """A small thread-safe cache whose entries expire, with a size cap so
    callers cycling through many distinct queries cannot grow it unbounded."""

    def __init__(self, ttl_seconds: float, max_entries: int = 256):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        if self.ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            stored_at, value = item
            if now - stored_at > self.ttl:
                del self._items[key]
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        if self.ttl <= 0:
            return
        now = time.monotonic()
        with self._lock:
            self._items[key] = (now, value)
            self._items.move_to_end(key)
            for stale_key in [k for k, (t, _) in self._items.items() if now - t > self.ttl]:
                del self._items[stale_key]
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class PrometheusAPI:
    def __init__(self, base_url: str, session: requests.Session, cache: TTLCache, timeout=(2, 15)):
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.cache = cache
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, Any]) -> list[dict]:
        key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        try:
            response = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            reason = "timed out" if isinstance(exc, requests.Timeout) else "connection failed"
            raise PrometheusError(f"Prometheus is unreachable ({reason})") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise PrometheusError(f"Prometheus returned HTTP {response.status_code} without JSON") from exc
        if payload.get("status") != "success":
            raise PrometheusError(f"Prometheus query failed: {payload.get('error', 'unknown error')}")
        result = payload.get("data", {}).get("result", [])
        self.cache.set(key, result)
        return result

    def instant(self, query: str, at: float | None = None) -> list[dict]:
        params: dict[str, Any] = {"query": query}
        if at is not None:
            params["time"] = f"{at:.3f}"
        return self._get("/api/v1/query", params)

    def range(self, query: str, start: float, end: float, step: int) -> list[dict]:
        return self._get(
            "/api/v1/query_range",
            {"query": query, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": str(step)},
        )


def vector_by_target(result: list[dict]) -> dict[str, float]:
    values: dict[str, float] = {}
    for entry in result:
        target = entry.get("metric", {}).get("target")
        sample = entry.get("value", [])
        if not target or len(sample) < 2:
            continue
        try:
            value = float(sample[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values[target] = value
    return values


def matrix_by_target(result: list[dict]) -> dict[str, dict[int, float]]:
    series: dict[str, dict[int, float]] = {}
    for entry in result:
        target = entry.get("metric", {}).get("target")
        if not target:
            continue
        points = series.setdefault(target, {})
        for ts, raw in entry.get("values", []):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                points[int(round(float(ts)))] = value
    return series


def windowed_counts(api: PrometheusAPI, window: str) -> tuple[dict[str, float], dict[str, float]]:
    success = vector_by_target(api.instant(f"sum by (target) (increase(ping_success_total[{window}]))"))
    failures = vector_by_target(api.instant(f"sum by (target) (increase(ping_failure_total[{window}]))"))
    return success, failures


def _ordered_targets(preferred: list[str], *found: Iterable[str]) -> list[str]:
    if preferred:
        return preferred
    names: set[str] = set()
    for group in found:
        names.update(group)
    return sorted(names)


def availability_history(
    api: PrometheusAPI, targets: list[str], window_seconds: int, buckets: int, now: float
) -> dict[str, Any]:
    """Split the window into equal buckets and report, per target, the share of
    samples in each bucket where the target was up.

    Bucket boundaries are aligned to multiples of the bucket size so they stay
    put between refreshes; the newest bucket is still filling up.
    """
    step = window_seconds // buckets
    if step < MIN_STEP_SECONDS:
        raise ValueError(
            f"Buckets of {window_seconds / buckets:g}s are finer than the {MIN_STEP_SECONDS}s scrape interval; "
            "use fewer buckets or a longer window."
        )
    end = math.ceil(now / step) * step
    first = end - (buckets - 1) * step

    series = matrix_by_target(
        api.range(f"avg by (target) (avg_over_time(ping_up[{step}s]))", first, end, step)
    )
    overall = vector_by_target(
        api.instant(f"avg by (target) (avg_over_time(ping_up[{window_seconds}s]))", at=now)
    )
    p95 = vector_by_target(
        api.instant(
            "histogram_quantile(0.95, sum by (target, le) "
            f"(rate(ping_latency_seconds_bucket[{window_seconds}s])))",
            at=now,
        )
    )

    eval_points = [first + i * step for i in range(buckets)]
    rows = []
    for target in _ordered_targets(targets, series, overall):
        points = series.get(target, {})
        rows.append(
            {
                "url": target,
                "availability": _pct(overall.get(target)),
                "latency_p95_ms": round(p95[target] * 1000, 1) if target in p95 else None,
                "buckets": [_pct(points.get(ts)) for ts in eval_points],
            }
        )
    return {
        "window_seconds": window_seconds,
        "bucket_seconds": step,
        "start": end - buckets * step,
        "end": end,
        "bucket_starts": [ts - step for ts in eval_points],
        "targets": rows,
    }


def find_outages(api: PrometheusAPI, targets: list[str], window_seconds: int, now: float) -> dict[str, Any]:
    """Find maximal runs of time where a target was down.

    Each evaluation point asks "was there any down sample in the last step?"
    (min_over_time), so even a single failed check is caught however coarse
    the step gets for long windows. Times are accurate to one step. A short gap
    in the data with the target down on both sides (an agent restart) does not
    split an outage; a longer gap ends it at the last down observation.
    """
    if window_seconds < 4 * MIN_STEP_SECONDS:
        raise ValueError("Outage windows must be at least 1m.")
    step = max(MIN_STEP_SECONDS, math.ceil(window_seconds / MAX_POINTS))
    end = math.floor(now / step) * step
    start = end - window_seconds
    series = matrix_by_target(
        api.range(f"min by (target) (min_over_time(ping_up[{step}s]))", start + step, end, step)
    )

    outages: list[dict[str, Any]] = []
    for target in _ordered_targets(targets, series):
        outages.extend(_runs_of_downtime(target, series.get(target, {}), step, start + step, end, now))
    outages.sort(key=lambda outage: outage["started_at"], reverse=True)
    return {"resolution_seconds": step, "start": start, "end": end, "outages": outages}


def _runs_of_downtime(
    target: str, points: dict[int, float], step: int, first: int, last: int, now: float
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    run_start: int | None = None
    run_end: int | None = None  # last evaluation point observed down

    def close(ongoing: bool) -> None:
        assert run_start is not None and run_end is not None
        started = run_start - step
        runs.append(
            {
                "url": target,
                "started_at": started,
                "ended_at": None if ongoing else run_end,
                "duration_seconds": int((now if ongoing else run_end) - started),
                "ongoing": ongoing,
                "started_before_window": run_start == first,
            }
        )

    for ts in range(first, last + 1, step):
        value = points.get(ts)
        if value is None:
            # No data: keep the run open across a short gap (an agent restart),
            # but never longer than we can vouch for.
            if run_start is not None and ts - run_end > BRIDGE_GAP_SECONDS:
                close(ongoing=False)
                run_start = run_end = None
            continue
        if value < 1:
            if run_start is None:
                run_start = ts
            run_end = ts
        elif run_start is not None:
            close(ongoing=False)
            run_start = run_end = None
    if run_start is not None:
        close(ongoing=run_end == last)
    return runs


def _pct(fraction: float | None) -> float | None:
    return round(fraction * 100, 2) if fraction is not None else None
