"""Response models. They exist mainly to publish an accurate OpenAPI contract
at /docs, so integrators never need to read the implementation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

State = Literal["up", "down", "unknown"]
Overall = Literal["operational", "degraded", "outage", "unknown"]


class Percentiles(BaseModel):
    p50: float | None
    p95: float | None
    p99: float | None


class ConnectionPhases(BaseModel):
    dns: float | None = Field(description="DNS lookup of the latest check, ms; null if the phase did not complete")
    tcp: float | None = Field(description="TCP connect of the latest check, ms")
    tls: float | None = Field(description="TLS handshake of the latest check, ms")


class TargetStatus(BaseModel):
    url: str
    name: str = Field(description="Host and path, for display")
    state: State = Field(
        description="up/down from the latest check; unknown when there is no data yet or it is stale"
    )
    up: bool = Field(description="Shorthand for state == 'up'")
    reason: str | None = Field(
        description=(
            "Outcome of the latest check: ok, http_4xx, http_5xx, timeout, dns, connection_refused, "
            "tls, network, error; 'pending' before the first check; 'not_monitored' if the agent "
            "does not check this URL"
        )
    )
    stale: bool = Field(description="True when the latest check is older than the agent's staleness budget")
    http_status: int | None = Field(description="Status code of the latest response; null if none was received")
    last_checked_at: str | None
    last_checked_seconds_ago: float | None
    state_since: str | None = Field(
        description="When the current up/down state began, as observed by the running agent"
    )
    latency_ms: float | None = Field(description="Response latency of the latest check")
    latency_percentiles_ms: Percentiles = Field(description="Since the agent started")
    connection_ms: ConnectionPhases
    availability: float | None = Field(
        description="Percent of successful checks since the agent started; null with no checks"
    )
    total_checks: int
    success: int
    failures: int
    retries: int = Field(description="Retry attempts since the agent started; steady growth means flaky")
    ssl_cert_expiry_days: float | None = Field(description="Negative once the certificate has expired")


class Summary(BaseModel):
    up: int
    down: int
    unknown: int
    total: int


class MonitorInfo(BaseModel):
    interval_seconds: float | None
    stale_after_seconds: float | None
    region: str | None


class StatusResponse(BaseModel):
    status: Overall = Field(
        description=(
            "outage: every target is down; degraded: some are down; unknown: none are down but at least "
            "one has no fresh data; operational: every target is up"
        )
    )
    summary: Summary
    generated_at: str
    monitor: MonitorInfo
    targets: list[TargetStatus]


class TargetRef(BaseModel):
    url: str
    name: str


class TargetsResponse(BaseModel):
    source: Literal["agent", "config"] = Field(
        description="'agent' when discovered from the ping-agent, 'config' when PING_TARGET_URLS is set on the gateway"
    )
    targets: list[TargetRef]


class UptimeRow(BaseModel):
    url: str
    success: float
    failures: float
    availability: str | None = Field(description="Formatted percentage such as '99.50%'; null with no checks")


class UptimeSummaryResponse(BaseModel):
    targets: list[UptimeRow]


class WindowedUptimeResponse(BaseModel):
    window: str
    targets: list[UptimeRow]


class HistoryTarget(BaseModel):
    url: str
    name: str
    availability: float | None = Field(description="Percent of samples up over the whole window")
    latency_p95_ms: float | None = Field(description="p95 response latency over the whole window")
    buckets: list[float | None] = Field(
        description="Percent of samples up per bucket, oldest first; null where there was no data"
    )


class HistoryResponse(BaseModel):
    window: str
    bucket_seconds: int
    start: str
    end: str
    bucket_starts: list[str]
    targets: list[HistoryTarget]


class Outage(BaseModel):
    url: str
    name: str
    started_at: str
    ended_at: str | None = Field(description="null while the outage is ongoing")
    duration_seconds: int
    ongoing: bool
    started_before_window: bool = Field(description="The outage may have begun before the window opened")


class OutageTotals(BaseModel):
    url: str
    name: str
    count: int
    downtime_seconds: int


class OutagesResponse(BaseModel):
    window: str
    resolution_seconds: int = Field(description="Start and end times are accurate to this many seconds")
    start: str
    end: str
    total: int
    truncated: bool
    by_target: list[OutageTotals]
    outages: list[Outage]
