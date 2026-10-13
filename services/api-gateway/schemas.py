"""Response models. They exist mainly to publish an accurate OpenAPI contract
at /docs, so integrators never need to read the implementation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

State = Literal["up", "down", "unknown"]
Overall = Literal["operational", "degraded", "outage", "unknown", "maintenance"]


class Percentiles(BaseModel):
    p50: float | None
    p95: float | None
    p99: float | None


class ConnectionPhases(BaseModel):
    dns: float | None = Field(description="DNS lookup of the latest check, ms; null if the phase did not complete")
    tcp: float | None = Field(description="TCP connect of the latest check, ms")
    tls: float | None = Field(description="TLS handshake of the latest check, ms")


class RegionStatus(BaseModel):
    region: str = Field(description="The agent's PING_REGION, or 'default' for a single unnamed agent")
    state: State
    reason: str | None
    slow: bool
    stale: bool
    http_status: int | None
    latency_ms: float | None
    last_checked_at: str | None
    last_checked_seconds_ago: float | None


class Maintenance(BaseModel):
    active: bool = Field(description="False for an upcoming window")
    starts_at: str
    ends_at: str
    comment: str


class TargetStatus(BaseModel):
    url: str = Field(description="The target's id: its URL, tcp:// or tls:// address, or dns://host/RECORD")
    name: str = Field(description="Configured name, or host and path")
    kind: Literal["http", "tcp", "dns"]
    state: State = Field(
        description=(
            "Majority verdict of the regions with fresh data: down when more than half see it down; "
            "unknown when no region has fresh data"
        )
    )
    up: bool = Field(description="Shorthand for state == 'up'")
    slow: bool = Field(description="Up, but most regions see it slower than its degraded_after threshold")
    reason: str | None = Field(
        description=(
            "Outcome of the latest check: ok, http_4xx, http_5xx, unexpected_status, body_mismatch, "
            "unexpected_answer, timeout, dns, connection_refused, tls, network, error; 'pending' before "
            "the first check; 'not_monitored' if no agent checks this target"
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
    interval_seconds: float | None
    stale_after_seconds: float | None
    slow_threshold_ms: float | None
    slo_objective: float | None = Field(description="Availability objective in percent, e.g. 99.9")
    regions_total: int = Field(description="Regions with fresh data for this target")
    regions_failing: int
    regions: list[RegionStatus]
    maintenance: Maintenance | None = Field(description="Active or next planned maintenance window")


class Summary(BaseModel):
    up: int
    down: int
    unknown: int
    slow: int
    maintenance: int = Field(description="Targets in an active maintenance window, counted apart from the rest")
    total: int


class AgentHealth(BaseModel):
    region: str | None
    reachable: bool


class MonitorInfo(BaseModel):
    interval_seconds: float | None = Field(description="Shortest check interval among the targets")
    agents: list[AgentHealth]
    alertmanager: Literal["reachable", "unreachable", "not_configured"]


class MaintenanceWindow(Maintenance):
    targets: list[str] = Field(description="Names of the targets in this window")


class StatusResponse(BaseModel):
    status: Overall = Field(
        description=(
            "outage: every target is down; degraded: some are down or slow; unknown: none are down but at "
            "least one has no fresh data; maintenance: every target is in a maintenance window; "
            "operational: every target is up. Targets in active maintenance are set aside."
        )
    )
    summary: Summary
    generated_at: str
    monitor: MonitorInfo
    maintenance: list[MaintenanceWindow]
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


class SLOTarget(BaseModel):
    url: str
    name: str
    objective: float | None = Field(description="Objective in percent")
    availability: float | None = Field(description="Percent of time up (region quorum) over the covered part of the window")
    coverage_seconds: int = Field(description="How much of the window has data; budgets on young installs are partial")
    allowed_downtime_seconds: int | None = Field(description="The error budget for the whole window")
    downtime_seconds: int | None
    budget_remaining: float | None = Field(description="Percent of the error budget left; negative once overspent")
    burn_rate_1h: float | None = Field(description="1 means spending the budget exactly as fast as it accrues")
    burn_rate_6h: float | None


class SLOResponse(BaseModel):
    window: str
    targets: list[SLOTarget]
