import http.server
import logging
import math
import os
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

import requests

API_DIR = os.path.dirname(__file__)
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

os.environ.pop("PING_TARGET_URLS", None)
logging.getLogger("iyup.api").setLevel(logging.ERROR)  # expected upstream failures are tested, not logged
import main  # noqa: E402
import promql  # noqa: E402
import snapshot as snap  # noqa: E402

try:
    from fastapi.testclient import TestClient
except RuntimeError:  # httpx missing: route tests are skipped, unit tests still run
    TestClient = None

NOW = 1_760_000_000.0
A, B = "https://a.example", "https://b.example"
PROM_URL = "http://prom.example"


def agent_text(now=NOW, stale_after=91.5, region=None, extra=""):
    """A realistic ping-agent exposition: A is up, B is down (refused)."""
    reg = f',region="{region}"' if region else ""
    return f"""
# HELP ping_interval_seconds Configured pause between check cycles
# TYPE ping_interval_seconds gauge
ping_interval_seconds{{{reg[1:]}}} 30
# TYPE ping_stale_after_seconds gauge
ping_stale_after_seconds{{{reg[1:]}}} {stale_after}
# TYPE ping_target_info gauge
ping_target_info{{target="{A}"{reg}}} 1
ping_target_info{{target="{B}"{reg}}} 1
# TYPE ping_up gauge
ping_up{{target="{A}"{reg}}} 1
ping_up{{target="{B}"{reg}}} 0
# TYPE ping_success_total counter
ping_success_total{{target="{A}"{reg}}} 90
ping_success_total{{target="{B}"{reg}}} 50
# TYPE ping_failure_total counter
ping_failure_total{{target="{A}"{reg}}} 10
ping_failure_total{{target="{B}"{reg}}} 50
# TYPE ping_retries_total counter
ping_retries_total{{target="{A}"{reg}}} 4
# TYPE ping_last_result gauge
ping_last_result{{reason="ok",target="{A}"{reg}}} 1
ping_last_result{{reason="connection_refused",target="{B}"{reg}}} 1
# TYPE ping_last_status_code gauge
ping_last_status_code{{target="{A}"{reg}}} 200
# TYPE ping_last_latency_seconds gauge
ping_last_latency_seconds{{target="{A}"{reg}}} 0.045
# TYPE ping_last_check_timestamp_seconds gauge
ping_last_check_timestamp_seconds{{target="{A}"{reg}}} {now - 12}
ping_last_check_timestamp_seconds{{target="{B}"{reg}}} {now - 12}
# TYPE ping_state_change_timestamp_seconds gauge
ping_state_change_timestamp_seconds{{target="{A}"{reg}}} {now - 3600}
ping_state_change_timestamp_seconds{{target="{B}"{reg}}} {now - 240}
# TYPE ping_ssl_cert_expiry_days gauge
ping_ssl_cert_expiry_days{{target="{A}"{reg}}} 57.94
# TYPE ping_dns_duration_seconds gauge
ping_dns_duration_seconds{{target="{A}"{reg}}} 0.003
# TYPE ping_connect_duration_seconds gauge
ping_connect_duration_seconds{{target="{A}"{reg}}} 0.011
ping_connect_duration_seconds{{target="{B}"{reg}}} 0.0002
# TYPE ping_tls_duration_seconds gauge
ping_tls_duration_seconds{{target="{A}"{reg}}} 0.019
# TYPE ping_latency_seconds histogram
ping_latency_seconds_bucket{{target="{A}",le="0.025"{reg}}} 10
ping_latency_seconds_bucket{{target="{A}",le="0.05"{reg}}} 60
ping_latency_seconds_bucket{{target="{A}",le="0.1"{reg}}} 90
ping_latency_seconds_bucket{{target="{A}",le="0.25"{reg}}} 95
ping_latency_seconds_bucket{{target="{A}",le="0.5"{reg}}} 98
ping_latency_seconds_bucket{{target="{A}",le="1"{reg}}} 100
ping_latency_seconds_bucket{{target="{A}",le="+Inf"{reg}}} 100
ping_latency_seconds_sum{{target="{A}"{reg}}} 6.2
ping_latency_seconds_count{{target="{A}"{reg}}} 100
{extra}""".replace("{}", "")


def response(json_body=None, text="", status=200):
    resp = Mock()
    resp.status_code = status
    resp.text = text
    resp.json = Mock(return_value=json_body) if json_body is not None else Mock(side_effect=ValueError("no json"))

    def raise_for_status():
        if status >= 400:
            raise requests.HTTPError(f"{status} error")

    resp.raise_for_status = raise_for_status
    return resp


class FakeUpstreams:
    """Stands in for both the ping-agent and Prometheus behind main.SESSION."""

    def __init__(self, metrics=None, instant=None, ranged=None, agent_down=False, prom_down=False):
        self.metrics = agent_text() if metrics is None else metrics
        self.instant = instant or (lambda query: [])
        self.ranged = ranged or (lambda query, start, end, step: [])
        self.agent_down = agent_down
        self.prom_down = prom_down
        self.queries = []

    def get(self, url, params=None, timeout=None):
        if url == main.PING_AGENT_METRICS_URL:
            if self.agent_down:
                raise requests.ConnectionError("connection refused")
            return response(text=self.metrics)
        if self.prom_down:
            raise requests.ConnectionError("prometheus refused")
        query = params["query"]
        self.queries.append(query)
        if url.endswith("/api/v1/query_range"):
            result = self.ranged(query, float(params["start"]), float(params["end"]), int(params["step"]))
        else:
            result = self.instant(query)
        if isinstance(result, dict):  # an error payload
            return response(json_body=result, status=400)
        return response(json_body={"status": "success", "data": {"result": result}})


def vector(values):
    return [{"metric": {"target": t}, "value": [NOW, str(v)]} for t, v in values.items()]


class GatewayState(unittest.TestCase):
    """Resets module-level configuration so tests cannot leak into each other."""

    def setUp(self):
        self.saved = (main.CONFIGURED_TARGETS, main.PROMETHEUS_URL)
        main.CONFIGURED_TARGETS = []
        main.PROMETHEUS_URL = PROM_URL
        main.PROM_CACHE.clear()

    def tearDown(self):
        main.CONFIGURED_TARGETS, main.PROMETHEUS_URL = self.saved
        main.PROM_CACHE.clear()

    def upstreams(self, **kwargs):
        fake = FakeUpstreams(**kwargs)
        patcher = patch.object(main.SESSION, "get", side_effect=fake.get)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


# ---------------------------------------------------------------- parsing


class TestAgentMetricsParsing(unittest.TestCase):
    def test_parses_every_metric(self):
        s = snap.parse_agent_metrics(agent_text())
        self.assertEqual(s.configured, [A, B])
        self.assertEqual(s.interval_seconds, 30)
        self.assertEqual(s.stale_after_seconds, 91.5)
        a, b = s.targets[A], s.targets[B]
        self.assertEqual((a.up, a.success, a.failures, a.retries), (1, 90, 10, 4))
        self.assertEqual((a.reason, a.status_code, a.last_latency), ("ok", 200, 0.045))
        self.assertEqual(b.reason, "connection_refused")
        self.assertIsNone(b.last_latency)
        self.assertEqual([le for le, _ in a.buckets], sorted(le for le, _ in a.buckets))
        self.assertEqual(a.buckets[-1], (math.inf, 100))

    def test_region_label_is_reported(self):
        s = snap.parse_agent_metrics(agent_text(region="sin"))
        self.assertEqual(s.region, "sin")
        self.assertEqual(s.targets[A].up, 1)

    def test_label_values_with_braces_and_commas(self):
        # The old hand-rolled parser split on the first "}" and broke here.
        odd = 'https://odd.example/a,b}c?q="x"'
        escaped = odd.replace('"', '\\"')
        text = f'# TYPE ping_up gauge\nping_up{{target="{escaped}"}} 1\n'
        s = snap.parse_agent_metrics(text)
        self.assertEqual(s.targets[odd].up, 1)

    def test_ignores_series_without_target(self):
        s = snap.parse_agent_metrics("# TYPE ping_up gauge\nping_up 1\n")
        self.assertEqual(s.targets, {})

    def test_malformed_text_raises(self):
        with self.assertRaises(snap.MetricsFormatError):
            snap.parse_agent_metrics('ping_up{target="x" 1\n')


class TestPercentiles(unittest.TestCase):
    BUCKETS = [(0.005, 0), (0.01, 0), (0.025, 10), (0.05, 60), (0.1, 90), (0.25, 95), (0.5, 98), (1.0, 100), (math.inf, 100)]

    def test_interpolates_within_buckets(self):
        p50 = snap.percentile_from_histogram(self.BUCKETS, 0.50)
        p95 = snap.percentile_from_histogram(self.BUCKETS, 0.95)
        self.assertAlmostEqual(p50, 0.025 + (40 / 50) * 0.025)
        self.assertAlmostEqual(p95, 0.25)

    def test_empty_and_zero(self):
        self.assertIsNone(snap.percentile_from_histogram([], 0.5))
        self.assertIsNone(snap.percentile_from_histogram([(0.1, 0), (math.inf, 0)], 0.5))


# ---------------------------------------------------------------- semantics


class TestTargetStatus(unittest.TestCase):
    def test_up_target(self):
        row = snap.target_status(A, snap.parse_agent_metrics(agent_text()), NOW)
        self.assertEqual((row["state"], row["up"], row["reason"], row["stale"]), ("up", True, "ok", False))
        self.assertEqual(row["last_checked_seconds_ago"], 12)
        self.assertEqual(row["state_since"], snap.iso(NOW - 3600))
        self.assertEqual(row["availability"], 90.0)
        self.assertEqual(row["connection_ms"], {"dns": 3.0, "tcp": 11.0, "tls": 19.0})
        self.assertEqual(row["ssl_cert_expiry_days"], 57.9)
        self.assertEqual(row["name"], "a.example")

    def test_down_target_explains_itself(self):
        row = snap.target_status(B, snap.parse_agent_metrics(agent_text()), NOW)
        self.assertEqual((row["state"], row["reason"]), ("down", "connection_refused"))
        self.assertIsNone(row["latency_ms"])
        self.assertIsNone(row["http_status"])
        self.assertEqual(row["state_since"], snap.iso(NOW - 240))

    def test_stale_data_is_unknown_not_up(self):
        row = snap.target_status(A, snap.parse_agent_metrics(agent_text(now=NOW - 600)), NOW)
        self.assertEqual((row["state"], row["up"], row["stale"]), ("unknown", False, True))
        self.assertEqual(row["reason"], "ok")  # last known outcome is kept
        self.assertIsNone(row["state_since"])

    def test_not_yet_checked_is_pending_not_down(self):
        text = f'# TYPE ping_target_info gauge\nping_target_info{{target="{A}"}} 1\n'
        row = snap.target_status(A, snap.parse_agent_metrics(text), NOW)
        self.assertEqual((row["state"], row["reason"]), ("unknown", "pending"))
        self.assertIsNone(row["availability"])  # no checks is not 0% available
        self.assertEqual(row["total_checks"], 0)

    def test_url_the_agent_does_not_check(self):
        row = snap.target_status("https://elsewhere.example", snap.parse_agent_metrics(agent_text()), NOW)
        self.assertEqual((row["state"], row["reason"]), ("unknown", "not_monitored"))

    def test_older_agent_without_freshness_metrics(self):
        text = f'# TYPE ping_up gauge\nping_up{{target="{A}"}} 1\n'
        row = snap.target_status(A, snap.parse_agent_metrics(text), NOW)
        self.assertEqual((row["state"], row["stale"], row["last_checked_at"]), ("up", False, None))

    def test_display_name(self):
        self.assertEqual(snap.display_name("https://api.example.com/health/"), "api.example.com/health")
        self.assertEqual(snap.display_name("http://127.0.0.1:9"), "127.0.0.1:9")


class TestOverallStatus(unittest.TestCase):
    def test_rules(self):
        cases = [
            (["up", "up"], "operational"),
            (["up", "down"], "degraded"),
            (["down", "down"], "outage"),
            (["down", "unknown"], "degraded"),
            (["up", "unknown"], "unknown"),
            ([], "unknown"),
        ]
        for states, want in cases:
            self.assertEqual(snap.overall_status(states), want, states)


# ---------------------------------------------------------------- Prometheus helpers


class TestDurations(unittest.TestCase):
    def test_valid(self):
        for text, seconds in [("30s", 30), ("5m", 300), ("24h", 86400), ("7d", 604800), ("2w", 1209600)]:
            self.assertEqual(promql.parse_duration(text), seconds)

    def test_invalid(self):
        for text in ["abc", "5mm", "1", "m5", "", "5m; drop table", "5m\n1h", "0m", "-5m"]:
            with self.assertRaises(ValueError, msg=text):
                promql.parse_duration(text)


class TestTTLCache(unittest.TestCase):
    def test_expiry_and_size_cap(self):
        cache = promql.TTLCache(ttl_seconds=60, max_entries=3)
        for i in range(5):
            cache.set(f"k{i}", i)
        self.assertIsNone(cache.get("k0"))
        self.assertEqual(cache.get("k4"), 4)
        with patch.object(promql.time, "monotonic", return_value=time.monotonic() + 120):
            self.assertIsNone(cache.get("k4"))

    def test_disabled_cache(self):
        cache = promql.TTLCache(ttl_seconds=0)
        cache.set("k", 1)
        self.assertIsNone(cache.get("k"))


class FakeAPI:
    def __init__(self, ranged=None, instant=None):
        self.ranged = ranged or (lambda *a: [])
        self.instant_fn = instant or (lambda q: [])
        self.calls = []

    def range(self, query, start, end, step):
        self.calls.append((query, start, end, step))
        return self.ranged(query, start, end, step)

    def instant(self, query, at=None):
        return self.instant_fn(query)


def matrix(target, points):
    return [{"metric": {"target": target}, "values": [[ts, str(v)] for ts, v in points]}]


class TestOutageDetection(unittest.TestCase):
    def run_outages(self, pattern, now=NOW, window=600):
        """pattern maps eval-point index -> value (missing = no data)."""
        def ranged(query, start, end, step):
            points = []
            for i, ts in enumerate(range(int(start), int(end) + 1, step)):
                if i in pattern:
                    points.append((ts, pattern[i]))
            return matrix(A, points)

        return promql.find_outages(FakeAPI(ranged), [A], window, now)

    def test_single_closed_outage(self):
        # 40 points of 15s; down at points 10..13 (one minute).
        data = self.run_outages({i: (0 if 10 <= i <= 13 else 1) for i in range(40)})
        self.assertEqual(len(data["outages"]), 1)
        outage = data["outages"][0]
        self.assertFalse(outage["ongoing"])
        self.assertEqual(outage["duration_seconds"], 60)
        self.assertEqual(outage["ended_at"] - outage["started_at"], 60)
        self.assertFalse(outage["started_before_window"])

    def test_ongoing_and_window_edge(self):
        pattern = {i: 1 for i in range(40)}
        pattern.update({0: 0, 1: 0, 38: 0, 39: 0})
        data = self.run_outages(pattern)
        ongoing, earliest = data["outages"]
        self.assertTrue(ongoing["ongoing"])
        self.assertIsNone(ongoing["ended_at"])
        self.assertTrue(earliest["started_before_window"])

    def test_data_gap_ends_an_outage_without_inventing_recovery(self):
        pattern = {i: 1 for i in range(40)}
        pattern.update({5: 0, 6: 0})
        del pattern[7]  # agent went quiet, then came back to an up target
        data = self.run_outages(pattern)
        self.assertEqual(len(data["outages"]), 1)
        self.assertEqual(data["outages"][0]["duration_seconds"], 30)

    def test_agent_restart_does_not_split_an_outage(self):
        # Regression: down throughout, with a 30s hole while the agent restarted.
        pattern = {i: 1 for i in range(40)}
        pattern.update({i: 0 for i in range(10, 30)})
        del pattern[18], pattern[19]
        data = self.run_outages(pattern)
        self.assertEqual(len(data["outages"]), 1)
        self.assertEqual(data["outages"][0]["duration_seconds"], 20 * 15)

    def test_long_silence_splits_an_outage(self):
        # Ten minutes without data is not something we can vouch for.
        pattern = {i: 0 for i in range(40)}
        for i in range(10, 50):
            pattern.pop(i, None)
        pattern.update({i: 0 for i in range(50, 60)})
        data = self.run_outages(pattern, window=900)
        self.assertEqual(len(data["outages"]), 2)

    def test_long_windows_stay_under_the_point_limit(self):
        api = FakeAPI()
        data = promql.find_outages(api, [A], 30 * 86400, NOW)
        _, start, end, step = api.calls[0]
        self.assertLessEqual((end - start) / step + 1, promql.MAX_POINTS + 1)
        self.assertIn(f"[{step}s]", api.calls[0][0])
        self.assertEqual(data["resolution_seconds"], step)


class TestAvailabilityHistory(unittest.TestCase):
    def test_buckets_are_aligned_and_gaps_stay_null(self):
        def ranged(query, start, end, step):
            # Every bucket but the second has data; the third is half down.
            ts = [int(start) + i * step for i in range(4)]
            return matrix(A, [(ts[0], 1), (ts[2], 0.5), (ts[3], 1)])

        def instant(query):
            if "histogram_quantile" in query:
                return vector({A: 0.2125})
            return vector({A: 0.875})

        data = promql.availability_history(FakeAPI(ranged, instant), [A, B], 4 * 3600, 4, NOW)
        self.assertEqual(data["bucket_seconds"], 3600)
        self.assertEqual(data["end"] % 3600, 0)
        self.assertGreaterEqual(data["end"], NOW)
        self.assertEqual(data["end"] - data["start"], 4 * 3600)
        self.assertEqual(data["bucket_starts"][0], data["start"])
        row_a, row_b = data["targets"]
        self.assertEqual(row_a["buckets"], [100.0, None, 50.0, 100.0])
        self.assertEqual((row_a["availability"], row_a["latency_p95_ms"]), (87.5, 212.5))
        self.assertEqual(row_b["buckets"], [None] * 4)  # configured target with no data yet

    def test_rejects_buckets_finer_than_scrapes(self):
        with self.assertRaises(ValueError):
            promql.availability_history(FakeAPI(), [A], 600, 100, NOW)


# ---------------------------------------------------------------- HTTP routes


@unittest.skipIf(TestClient is None, "httpx is not installed (pip install -r requirements-dev.txt)")
class TestRoutes(GatewayState):
    def setUp(self):
        super().setUp()
        self.client = TestClient(main.app)
        clock = patch.object(main.time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def test_status(self):
        self.upstreams()
        body = self.client.get("/status").json()
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["summary"], {"up": 1, "down": 1, "unknown": 0, "total": 2})
        self.assertEqual(body["monitor"], {"interval_seconds": 30, "stale_after_seconds": 91.5, "region": None})
        self.assertEqual([t["url"] for t in body["targets"]], [A, B])
        self.assertEqual(body["targets"][1]["reason"], "connection_refused")

    def test_status_at_startup_is_unknown_not_degraded(self):
        # Regression: before the first check, targets used to read as down.
        self.upstreams(metrics=f'# TYPE ping_target_info gauge\nping_target_info{{target="{A}"}} 1\n')
        body = self.client.get("/status").json()
        self.assertEqual(body["status"], "unknown")
        self.assertEqual(body["targets"][0]["reason"], "pending")
        self.assertIsNone(body["targets"][0]["availability"])

    def test_status_when_agent_is_unreachable(self):
        self.upstreams(agent_down=True)
        resp = self.client.get("/status")
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["detail"], "ping-agent is unreachable (connection failed)")

    def test_configured_targets_override_discovery(self):
        main.CONFIGURED_TARGETS = [B, "https://new.example"]
        self.upstreams()
        body = self.client.get("/status").json()
        self.assertEqual([t["url"] for t in body["targets"]], [B, "https://new.example"])
        self.assertEqual(body["targets"][1]["reason"], "not_monitored")
        self.assertEqual(self.client.get("/targets").json()["source"], "config")

    def test_targets_discovered_from_agent(self):
        self.upstreams()
        body = self.client.get("/targets").json()
        self.assertEqual(body, {"source": "agent", "targets": [{"url": A, "name": "a.example"}, {"url": B, "name": "b.example"}]})

    def test_target_detail(self):
        self.upstreams()
        resp = self.client.get("/targets/https%3A%2F%2Fa.example")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((resp.json()["success"], resp.json()["failures"]), (90, 10))
        self.assertEqual(self.client.get("/targets/https%3A%2F%2Fnope.example").status_code, 404)

    def test_uptime_summary(self):
        self.upstreams(metrics=agent_text(extra=f'ping_target_info{{target="https://fresh.example"}} 1\n'))
        rows = {r["url"]: r for r in self.client.get("/uptime-summary").json()["targets"]}
        self.assertEqual(rows[A]["availability"], "90.00%")
        self.assertIsNone(rows["https://fresh.example"]["availability"])

    def test_windowed_uptime(self):
        self.upstreams(
            instant=lambda q: vector({A: 3, B: 2}) if "ping_success_total" in q else vector({A: 1, B: 2})
        )
        body = self.client.get("/uptime-summary-windowed?window=5m").json()
        self.assertEqual([r["availability"] for r in body["targets"]], ["75.00%", "50.00%"])

    def test_windowed_uptime_errors(self):
        self.upstreams(prom_down=True)
        self.assertEqual(self.client.get("/uptime-summary-windowed?window=0m").status_code, 400)
        self.assertEqual(self.client.get("/uptime-summary-windowed?window=5m;drop").status_code, 400)
        resp = self.client.get("/uptime-summary-windowed?window=5m")
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["detail"], "Prometheus is unreachable (connection failed)")
        main.PROMETHEUS_URL = ""
        self.assertEqual(self.client.get("/uptime-summary-windowed?window=5m").status_code, 501)

    def test_prometheus_query_error_is_reported(self):
        self.upstreams(instant=lambda q: {"status": "error", "error": "parse error"})
        resp = self.client.get("/uptime-summary-windowed?window=5m")
        self.assertEqual(resp.status_code, 502)
        self.assertIn("parse error", resp.json()["detail"])

    def test_history(self):
        def ranged(query, start, end, step):
            return matrix(A, [(int(start) + i * step, 1) for i in range(48)])

        fake = self.upstreams(ranged=ranged, instant=lambda q: vector({A: 1}) if "avg_over_time" in q else [])
        body = self.client.get("/history?window=24h&buckets=48").json()
        self.assertEqual(body["bucket_seconds"], 1800)
        self.assertEqual(len(body["bucket_starts"]), 48)
        self.assertTrue(body["start"].endswith("Z"))
        row_a, row_b = body["targets"]
        self.assertEqual((row_a["name"], row_a["availability"]), ("a.example", 100.0))
        self.assertEqual(row_a["buckets"], [100.0] * 48)
        self.assertEqual(row_b["buckets"], [None] * 48)
        # A second request inside the cache TTL does not hit Prometheus again.
        sent = len(fake.queries)
        self.client.get("/history?window=24h&buckets=48")
        self.assertEqual(len(fake.queries), sent)

    def test_history_validation(self):
        self.upstreams()
        self.assertEqual(self.client.get("/history?window=10m&buckets=200").status_code, 400)
        self.assertEqual(self.client.get("/history?window=60d").status_code, 400)
        self.assertEqual(self.client.get("/history?buckets=0").status_code, 422)
        self.assertEqual(self.client.get("/outages?window=30s").status_code, 400)

    def test_history_falls_back_to_prometheus_targets_when_agent_is_down(self):
        self.upstreams(agent_down=True, ranged=lambda q, s, e, st: matrix(B, [(int(s), 1)]))
        body = self.client.get("/history?window=1h&buckets=4").json()
        self.assertEqual([t["url"] for t in body["targets"]], [B])

    def test_outages(self):
        def ranged(query, start, end, step):
            values = []
            for i, ts in enumerate(range(int(start), int(end) + 1, step)):
                down = 10 <= i <= 13 or 30 <= i <= 31 or 90 <= i <= 95
                values.append((ts, 0 if down else 1))
            return matrix(A, values)

        self.upstreams(ranged=ranged)
        body = self.client.get("/outages?window=24h&limit=2").json()
        self.assertEqual(body["total"], 3)
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["outages"]), 2)
        self.assertEqual(body["by_target"][0]["count"], 3)
        newest = body["outages"][0]
        self.assertFalse(newest["ongoing"])  # the last run ends long before "now"
        self.assertTrue(newest["started_at"].endswith("Z"))

    def test_request_metrics_use_route_templates(self):
        self.upstreams()
        self.client.get("/targets/https%3A%2F%2Fa.example")
        self.client.get("/wp-admin/setup.php")
        self.client.get("/assets/status.css")
        self.client.get("/openapi.json")
        self.client.post("/status")
        exposition = self.client.get("/metrics").text
        self.assertIn('path="/targets/{target_url:path}"', exposition)
        self.assertIn('path="unmatched",status="404"', exposition)
        self.assertIn('path="/assets",status="200"', exposition)
        self.assertIn('path="/openapi.json",status="200"', exposition)
        self.assertIn('path="/status",status="405"', exposition)
        self.assertNotIn('path="unmatched",status="200"', exposition)
        self.assertNotIn("a.example", exposition)
        self.assertNotIn("wp-admin", exposition)

    def test_status_page(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("script-src 'self'", resp.headers["content-security-policy"])
        self.assertIn('src="/assets/status.js"', resp.text)
        js = self.client.get("/assets/status.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("javascript", js.headers["content-type"])
        self.assertEqual(js.headers["cache-control"], "no-cache")
        self.assertIn("etag", js.headers)
        self.assertEqual(self.client.get("/assets/status.css").status_code, 200)

    def test_openapi_documents_the_contract(self):
        schema = self.client.get("/openapi.json").json()
        for path in ["/status", "/history", "/outages", "/targets/{target_url}"]:
            self.assertIn(path, schema["paths"])
        self.assertIn("TargetStatus", schema["components"]["schemas"])


class TestAgentOverRealHTTP(GatewayState):
    """End to end over a socket: proves the gateway's HTTP client, timeout and
    parsing work against a real server, not just a mock."""

    def test_status_against_live_metrics_server(self):
        body = agent_text(now=time.time()).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        try:
            server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        except OSError as exc:
            self.skipTest(f"cannot bind a local test server: {exc}")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        saved = main.PING_AGENT_METRICS_URL
        try:
            main.PING_AGENT_METRICS_URL = f"http://127.0.0.1:{server.server_port}/metrics"
            payload = main.status()
        finally:
            main.PING_AGENT_METRICS_URL = saved
            server.shutdown()
            thread.join()
            server.server_close()
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["targets"][0]["state"], "up")


if __name__ == "__main__":
    unittest.main()
