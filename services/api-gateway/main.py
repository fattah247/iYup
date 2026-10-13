import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from requests.adapters import HTTPAdapter
from starlette.routing import Match
from urllib3.util.retry import Retry

import maintenance as mw
import promql
import snapshot as snap
from schemas import (
    HistoryResponse,
    OutagesResponse,
    SLOResponse,
    StatusResponse,
    TargetsResponse,
    TargetStatus,
    UptimeSummaryResponse,
    WindowedUptimeResponse,
)

load_dotenv()
log = logging.getLogger("iyup.api")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="iYup API",
    description=(
        "Uptime, latency, outages and error budgets for the targets checked by iYup's ping-agents. "
        "Current state comes straight from the agents (a majority vote when there are several); "
        "history comes from Prometheus; planned maintenance comes from Alertmanager silences."
    ),
    version="3.0.0",
)

ALLOWED_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# One or more agents, comma-separated: each is a region.
PING_AGENT_METRICS_URLS = [
    u.strip() for u in os.getenv("PING_AGENT_METRICS_URL", "http://ping-agent:8080/metrics").split(",") if u.strip()
]
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "").strip()
ALERTMANAGER_URL = os.getenv("ALERTMANAGER_URL", "").strip()
PROMETHEUS_QUERY_CACHE_SECONDS = int(os.getenv("PROMETHEUS_QUERY_CACHE_SECONDS", "15"))
# Optional: when set, the gateway reports exactly these targets in this order.
# When unset, it reports whatever the agents check, so the two cannot drift.
CONFIGURED_TARGETS = [t.strip() for t in os.getenv("PING_TARGET_URLS", "").split(",") if t.strip()]

MAX_HISTORY_SECONDS = 31 * 86400
MAX_SLO_SECONDS = 90 * 86400
AGENT_TIMEOUT = (2, 5)

# Upstreams are on the same network: fail fast and retry only what is cheap to
# retry, so a hung dependency costs callers seconds, not a minute.
SESSION = requests.Session()
_adapter = HTTPAdapter(
    max_retries=Retry(
        total=2,
        connect=2,
        read=0,
        status=2,
        backoff_factor=0.2,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    ),
    pool_connections=10,
    pool_maxsize=20,
)
SESSION.mount("http://", _adapter)
SESSION.mount("https://", _adapter)
PROM_CACHE = promql.TTLCache(PROMETHEUS_QUERY_CACHE_SECONDS)
SILENCE_CACHE = promql.TTLCache(15)
_AGENT_REGIONS: dict[str, str] = {}  # last region seen per agent URL, for health reporting

REQUEST_COUNT = Counter(
    "api_gateway_requests_total",
    "Total HTTP requests received by api-gateway",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "api_gateway_request_duration_seconds",
    "Request latency in seconds for api-gateway",
    ["method", "path"],
)


@app.middleware("http")
async def record_metrics(request: Request, call_next):
    if request.url.path == "/metrics":
        return await call_next(request)

    path = route_label(request)
    start = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - start

    REQUEST_COUNT.labels(method=request.method, path=path, status=str(response.status_code)).inc()
    REQUEST_LATENCY.labels(method=request.method, path=path).observe(duration)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if path == "/assets":
        # Revalidate on every load (a cheap 304 via ETag) so an upgraded gateway
        # never serves last version's page script against the new API.
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


def route_label(request: Request) -> str:
    """Label requests by route template, never the raw path: /targets/{target_url:path}
    and scanner noise would otherwise mint a new time series per URL. Matching
    against the router itself covers every route kind (API routes, the docs
    routes, static mounts), not just those that record scope["route"]."""
    scope = {
        "type": "http",
        "path": request.scope["path"],
        "root_path": request.scope.get("root_path", ""),
        "method": request.method,
    }
    partial = None
    for route in request.app.router.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route.path
        if match is Match.PARTIAL and partial is None:
            partial = route.path  # right path, wrong method (405)
    return partial or "unmatched"


def describe_upstream_error(exc: requests.RequestException) -> str:
    """A short, stable reason for an upstream failure; the full exception goes to the log."""
    if isinstance(exc, requests.Timeout):
        return "timed out"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.ConnectionError):
        return "connection failed"
    return type(exc).__name__


def _fetch_one(url: str) -> snap.AgentSnapshot | str:
    try:
        response = SESSION.get(url, timeout=AGENT_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("ping-agent fetch from %s failed: %s", url, exc)
        return describe_upstream_error(exc)
    if not response.text:
        return "empty metrics page"
    try:
        snapshot = snap.parse_agent_metrics(response.text)
    except snap.MetricsFormatError as exc:
        log.warning("ping-agent at %s returned %s", url, exc)
        return "unparseable metrics"
    _AGENT_REGIONS[url] = snapshot.region
    return snapshot


def fetch_snapshots() -> tuple[list[snap.AgentSnapshot], list[dict]]:
    """Fetch every agent in parallel. Partial failure is fine (the reachable
    regions still vote); total failure is a 502."""
    with ThreadPoolExecutor(max_workers=max(1, len(PING_AGENT_METRICS_URLS))) as pool:
        results = list(pool.map(_fetch_one, PING_AGENT_METRICS_URLS))
    snapshots = [r for r in results if isinstance(r, snap.AgentSnapshot)]
    health = [
        {
            "region": r.region if isinstance(r, snap.AgentSnapshot) else _AGENT_REGIONS.get(url),
            "reachable": isinstance(r, snap.AgentSnapshot),
        }
        for url, r in zip(PING_AGENT_METRICS_URLS, results)
    ]
    if not snapshots:
        reasons = sorted({r for r in results if isinstance(r, str)})
        raise HTTPException(status_code=502, detail=f"ping-agent is unreachable ({', '.join(reasons)})")
    return snapshots, health


def resolve_targets(snapshots: list[snap.AgentSnapshot]) -> list[str]:
    if CONFIGURED_TARGETS:
        return CONFIGURED_TARGETS
    names: dict[str, str] = {}
    for s in snapshots:
        for url in s.target_urls():
            names.setdefault(url, s.configured.get(url, {}).get("name") or snap.display_name(url))
        for url, info in s.configured.items():
            if info.get("name"):
                names[url] = info["name"]
    return sorted(names, key=lambda url: (names[url].lower(), url))


def current_targets_for_history() -> tuple[list[str], dict[str, str]]:
    """Targets (and their names) to report history for. History can outlive
    the agents, so if none is reachable fall back to whatever targets
    Prometheus has data for."""
    if CONFIGURED_TARGETS:
        return CONFIGURED_TARGETS, {}
    try:
        snapshots = fetch_snapshots()[0]
    except HTTPException:
        return [], {}
    return resolve_targets(snapshots), _names(snapshots)


def _name(url: str, names: dict[str, str]) -> str:
    return names.get(url) or snap.display_name(url)


def maintenance_windows() -> tuple[list[dict], str]:
    if not ALERTMANAGER_URL:
        return [], "not_configured"
    cached = SILENCE_CACHE.get("silences")
    if cached is not None:
        return cached, "reachable"
    try:
        response = SESSION.get(f"{ALERTMANAGER_URL.rstrip('/')}/api/v2/silences", timeout=AGENT_TIMEOUT)
        response.raise_for_status()
        windows = mw.windows_from_silences(response.json())
    except (requests.RequestException, ValueError) as exc:
        log.warning("Alertmanager silences unavailable: %s", exc)
        return [], "unreachable"
    SILENCE_CACHE.set("silences", windows)
    return windows, "reachable"


def _maintenance_view(window: dict) -> dict:
    return {
        "active": window["active"],
        "starts_at": snap.iso(window["starts_at"]),
        "ends_at": snap.iso(window["ends_at"]),
        "comment": window["comment"],
    }


def prometheus() -> promql.PrometheusAPI:
    if not PROMETHEUS_URL:
        raise HTTPException(status_code=501, detail="PROMETHEUS_URL is not configured")
    return promql.PrometheusAPI(PROMETHEUS_URL, SESSION, PROM_CACHE)


def parse_window(window: str, max_seconds: int | None = None) -> int:
    try:
        seconds = promql.parse_duration(window)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if max_seconds is not None and seconds > max_seconds:
        raise HTTPException(status_code=400, detail=f"Window '{window}' is longer than the {max_seconds // 86400}d maximum.")
    return seconds


def _names(snapshots: list[snap.AgentSnapshot]) -> dict[str, str]:
    names: dict[str, str] = {}
    for s in snapshots:
        for url, info in s.configured.items():
            if info.get("name"):
                names[url] = info["name"]
    return names


@app.get("/", include_in_schema=False)
def status_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Content-Security-Policy": (
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-cache",
        },
    )


app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz", summary="Liveness of the API process")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status", response_model=StatusResponse, summary="Current state of every target")
def status():
    snapshots, health = fetch_snapshots()
    windows, alertmanager = maintenance_windows()
    now = time.time()
    rows = []
    for url in resolve_targets(snapshots):
        row = snap.target_status(url, snapshots, now)
        window = mw.maintenance_for(url, windows)
        row["maintenance"] = _maintenance_view(window) if window else None
        rows.append(row)

    by_url = {row["url"]: row["name"] for row in rows}
    published = []
    for window in sorted(windows, key=lambda w: (not w["active"], w["starts_at"])):
        names = [name for url, name in by_url.items() if mw.applies_to(window, url)]
        if names:
            published.append({**_maintenance_view(window), "targets": names})

    intervals = [row["interval_seconds"] for row in rows if row["interval_seconds"]]
    return {
        "status": snap.overall_status(rows),
        "summary": snap.summarize(rows),
        "generated_at": snap.iso(now),
        "monitor": {
            "interval_seconds": min(intervals) if intervals else None,
            "agents": health,
            "alertmanager": alertmanager,
        },
        "maintenance": published,
        "targets": rows,
    }


@app.get("/targets", response_model=TargetsResponse, summary="Targets being reported")
def targets():
    if CONFIGURED_TARGETS:
        return {"source": "config", "targets": [{"url": u, "name": snap.display_name(u)} for u in CONFIGURED_TARGETS]}
    snapshots, _ = fetch_snapshots()
    names = _names(snapshots)
    return {
        "source": "agent",
        "targets": [{"url": u, "name": names.get(u) or snap.display_name(u)} for u in resolve_targets(snapshots)],
    }


@app.get("/targets/{target_url:path}", response_model=TargetStatus, summary="Current state of one target")
def target_detail(target_url: str):
    target_url = unquote(target_url)
    if CONFIGURED_TARGETS and target_url not in CONFIGURED_TARGETS:
        raise HTTPException(status_code=404, detail=f"Target '{target_url}' is not monitored")
    snapshots, _ = fetch_snapshots()
    if target_url not in resolve_targets(snapshots):
        raise HTTPException(status_code=404, detail=f"Target '{target_url}' is not monitored")
    row = snap.target_status(target_url, snapshots, time.time())
    window = mw.maintenance_for(target_url, maintenance_windows()[0])
    row["maintenance"] = _maintenance_view(window) if window else None
    return row


def _uptime_rows(urls: list[str], success: dict[str, float], failures: dict[str, float]) -> list[dict]:
    rows = []
    for url in urls:
        ok, bad = success.get(url, 0.0), failures.get(url, 0.0)
        pct = snap.availability_percent(ok, bad)
        rows.append(
            {"url": url, "success": ok, "failures": bad, "availability": f"{pct:.2f}%" if pct is not None else None}
        )
    return rows


@app.get("/uptime-summary", response_model=UptimeSummaryResponse, summary="Check counts since the agents started")
def uptime_summary():
    snapshots, _ = fetch_snapshots()
    success: dict[str, float] = {}
    failures: dict[str, float] = {}
    for s in snapshots:
        for url, m in s.targets.items():
            success[url] = success.get(url, 0.0) + m.success
            failures[url] = failures.get(url, 0.0) + m.failures
    return {"targets": _uptime_rows(resolve_targets(snapshots), success, failures)}


@app.get(
    "/uptime-summary-windowed",
    response_model=WindowedUptimeResponse,
    summary="Check counts over a recent window",
    description="Counts come from Prometheus increase(), which extrapolates, so short windows can show fractions.",
)
def uptime_summary_windowed(window: str = "5m"):
    parse_window(window)
    api = prometheus()
    try:
        success, failures = promql.windowed_counts(api, window)
    except promql.PrometheusError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    urls = current_targets_for_history()[0] or sorted(set(success) | set(failures))
    return {"window": window, "targets": _uptime_rows(urls, success, failures)}


@app.get(
    "/history",
    response_model=HistoryResponse,
    summary="Availability per time bucket",
    description=(
        "Splits the window into equal buckets and reports the percent of time each target was up "
        "(by region majority) in each bucket: the data behind an uptime bar chart. Bucket boundaries "
        "are aligned so they stay stable between refreshes; the newest bucket is still filling."
    ),
)
def history(
    window: str = "24h",
    buckets: int = Query(48, ge=1, le=200, description="Number of buckets the window is split into"),
):
    window_seconds = parse_window(window, MAX_HISTORY_SECONDS)
    api = prometheus()
    urls, names = current_targets_for_history()
    try:
        data = promql.availability_history(api, urls, window_seconds, buckets, time.time())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except promql.PrometheusError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "window": window,
        "bucket_seconds": data["bucket_seconds"],
        "start": snap.iso(data["start"]),
        "end": snap.iso(data["end"]),
        "bucket_starts": [snap.iso(ts) for ts in data["bucket_starts"]],
        "targets": [{**row, "name": _name(row["url"], names)} for row in data["targets"]],
    }


@app.get(
    "/outages",
    response_model=OutagesResponse,
    summary="Periods when targets were down",
    description=(
        "Derived from the region-majority verdict, so blips a retry absorbed and failures seen from only "
        "a minority of regions are not outages. Newest first; times are accurate to resolution_seconds."
    ),
)
def outages(
    window: str = "24h",
    limit: int = Query(50, ge=1, le=500, description="Maximum outages to list; totals always cover all of them"),
):
    window_seconds = parse_window(window, MAX_HISTORY_SECONDS)
    api = prometheus()
    now = time.time()
    urls, names = current_targets_for_history()
    try:
        data = promql.find_outages(api, urls, window_seconds, now)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except promql.PrometheusError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    found = data["outages"]
    totals: dict[str, dict] = {}
    for outage in found:
        entry = totals.setdefault(
            outage["url"], {"url": outage["url"], "name": _name(outage["url"], names), "count": 0, "downtime_seconds": 0}
        )
        entry["count"] += 1
        entry["downtime_seconds"] += outage["duration_seconds"]
    return {
        "window": window,
        "resolution_seconds": data["resolution_seconds"],
        "start": snap.iso(data["start"]),
        "end": snap.iso(data["end"]),
        "total": len(found),
        "truncated": len(found) > limit,
        "by_target": sorted(totals.values(), key=lambda entry: -entry["downtime_seconds"]),
        "outages": [
            {
                **outage,
                "name": _name(outage["url"], names),
                "started_at": snap.iso(outage["started_at"]),
                "ended_at": snap.iso(outage["ended_at"]),
            }
            for outage in found[:limit]
        ],
    }


@app.get(
    "/slo",
    response_model=SLOResponse,
    summary="Error budget per target",
    description=(
        "Availability against each target's objective over a rolling window (30 days by default): "
        "the allowed downtime, how much has been used, what is left, and how fast it is being spent. "
        "coverage_seconds says how much of the window has data."
    ),
)
def slo(window: str = "30d"):
    window_seconds = parse_window(window, MAX_SLO_SECONDS)
    if window_seconds < 3600:
        raise HTTPException(status_code=400, detail="SLO windows must be at least 1h.")
    api = prometheus()
    urls, names = current_targets_for_history()
    try:
        data = promql.slo_report(api, urls, window_seconds, time.time())
    except promql.PrometheusError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"window": window, "targets": [{**row, "name": _name(row["url"], names)} for row in data["targets"]]}
