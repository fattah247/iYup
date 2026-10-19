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

# main calls load_dotenv(), which would read a developer's .env; values already
# in the environment win, so pin the ones that change behavior.
os.environ.update({"PING_TARGET_URLS": "", "PROMETHEUS_QUERY_CACHE_SECONDS": "15"})
logging.getLogger("iyup.api").setLevel(logging.ERROR)  # expected upstream failures are tested, not logged
import main  # noqa: E402
import maintenance as mw  # noqa: E402
import promql  # noqa: E402
import snapshot as snap  # noqa: E402

try:
    from fastapi.testclient import TestClient
except RuntimeError:  # httpx missing: route tests are skipped, unit tests still run
    TestClient = None

NOW = 1_760_000_000.0
A, B = "https://a.example", "https://b.example"
PROM_URL = "http://prom.example"
AM_URL = "http://alertmanager.example"
AGENT_1 = "http://agent-1/metrics"
AGENT_2 = "http://agent-2/metrics"


def agent_text(now=NOW, region=None, a_up=1, b_up=0, a_slow=0, per_target=True):
    """A realistic ping-agent exposition: A is up, B is down (refused)."""
    reg = f',region="{region}"' if region else ""

    def series(name, target, value, more=""):
        return f'{name}{{target="{target}"{more}{reg}}} {value}\n'

    out = f'ping_target_info{{target="{A}",name="Alpha",kind="http"{reg}}} 1\n'
    out += f'ping_target_info{{target="{B}",name="",kind="http"{reg}}} 1\n'
    if per_target:
        for t in (A, B):
            out += series("ping_interval_seconds", t, 30) + series("ping_stale_after_seconds", t, 91.5)
            out += series("ping_slo_objective_ratio", t, 0.999)
        out += series("ping_degraded_threshold_seconds", A, 0.5)
    else:  # an agent from before per-target settings
        labels = f"{{{reg[1:]}}}" if region else ""
        out += f"ping_interval_seconds{labels} 30\nping_stale_after_seconds{labels} 91.5\n"
    out += series("ping_up", A, a_up) + series("ping_up", B, b_up)
    out += series("ping_degraded", A, a_slow)
    out += series("ping_success_total", A, 90) + series("ping_success_total", B, 50)
    out += series("ping_failure_total", A, 10) + series("ping_failure_total", B, 50)
    out += series("ping_retries_total", A, 4)
    out += series("ping_last_result", A, 1, ',reason="ok"' if a_up else ',reason="timeout"')
    out += series("ping_last_result", B, 1, ',reason="connection_refused"' if not b_up else ',reason="ok"')
    out += series("ping_last_status_code", A, 200)
    out += series("ping_last_latency_seconds", A, 0.045)
    out += series("ping_last_check_timestamp_seconds", A, now - 12) + series("ping_last_check_timestamp_seconds", B, now - 12)
    out += series("ping_state_change_timestamp_seconds", A, now - 3600) + series("ping_state_change_timestamp_seconds", B, now - 240)
    out += series("ping_ssl_cert_expiry_days", A, 57.94)
    out += series("ping_dns_duration_seconds", A, 0.003) + series("ping_connect_duration_seconds", A, 0.011)
    out += series("ping_tls_duration_seconds", A, 0.019)
    for le, count in [("0.025", 10), ("0.05", 60), ("0.1", 90), ("0.25", 95), ("0.5", 98), ("1", 100), ("+Inf", 100)]:
        out += f'ping_latency_seconds_bucket{{target="{A}",le="{le}"{reg}}} {count}\n'
    return out


def region_text(region, target=A, up=0, reason="timeout", status=None, name="Alpha"):
    """One region's minimal view of one freshly checked target."""
    labels = f'target="{target}",region="{region}"'
    out = f'ping_target_info{{target="{target}",name="{name}",kind="http",region="{region}"}} 1\n'
    out += f"ping_up{{{labels}}} {up}\nping_last_check_timestamp_seconds{{{labels}}} {NOW - 5}\n"
    out += f'ping_last_result{{{labels},reason="{reason}"}} 1\n'
    if status:
        out += f"ping_last_status_code{{{labels}}} {status}\n"
    return out


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
    """Stands in for the agents, Prometheus and Alertmanager behind main.SESSION."""

    def __init__(self, agents=None, instant=None, ranged=None, silences=None, prom_down=False, am_down=False):
        self.agents = {AGENT_1: agent_text()} if agents is None else agents  # url -> text, or None when down
        self.instant = instant or (lambda query: [])
        self.ranged = ranged or (lambda query, start, end, step: [])
        self.silences = silences or []
        self.prom_down = prom_down
        self.am_down = am_down
        self.queries = []

    def get(self, url, params=None, timeout=None):
        if url in self.agents:
            if self.agents[url] is None:
                raise requests.ConnectionError("connection refused")
            return response(text=self.agents[url])
        if url.startswith(AM_URL):
            if self.am_down:
                raise requests.ConnectionError("alertmanager refused")
            return response(json_body=self.silences)
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


def silence(target, state="active", comment="[maintenance] Database upgrade", regex=False, equal=True):
    return {
        "id": "s1",
        "status": {"state": state},
        "matchers": [{"name": "target", "value": target, "isRegex": regex, "isEqual": equal}],
        "startsAt": snap.iso(NOW - 600 if state == "active" else NOW + 3600),
        "endsAt": snap.iso(NOW + 3600 if state == "active" else NOW + 7200),
        "createdBy": "alice@example.com",
        "comment": comment,
    }


class GatewayState(unittest.TestCase):
    """Resets module-level configuration so tests cannot leak into each other."""

    def setUp(self):
        self.saved = (main.CONFIGURED_TARGETS, main.PROMETHEUS_URL, main.ALERTMANAGER_URL, main.PING_AGENT_METRICS_URLS)
        main.CONFIGURED_TARGETS = []
        main.PROMETHEUS_URL = PROM_URL
        main.ALERTMANAGER_URL = AM_URL
        main.PING_AGENT_METRICS_URLS = [AGENT_1]
        main.PROM_CACHE.clear()
        main.SILENCE_CACHE.clear()

    def tearDown(self):
        main.CONFIGURED_TARGETS, main.PROMETHEUS_URL, main.ALERTMANAGER_URL, main.PING_AGENT_METRICS_URLS = self.saved
        main.PROM_CACHE.clear()
        main.SILENCE_CACHE.clear()

    def upstreams(self, **kwargs):
        fake = FakeUpstreams(**kwargs)
        if "agents" in kwargs:
            main.PING_AGENT_METRICS_URLS = list(kwargs["agents"])
        patcher = patch.object(main.SESSION, "get", side_effect=fake.get)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


def snapshots(*texts):
    return [snap.parse_agent_metrics(t) for t in texts]


# ---------------------------------------------------------------- parsing


class TestAgentMetricsParsing(unittest.TestCase):
    def test_parses_every_metric(self):
        s = snap.parse_agent_metrics(agent_text())
        self.assertEqual(s.configured[A], {"name": "Alpha", "kind": "http"})
        self.assertEqual(s.region, snap.DEFAULT_REGION)
        a, b = s.targets[A], s.targets[B]
        self.assertEqual((a.up, a.success, a.failures, a.retries), (1, 90, 10, 4))
        self.assertEqual((a.reason, a.status_code, a.last_latency), ("ok", 200, 0.045))
        self.assertEqual((a.interval, a.stale_after, a.slo, a.degraded_threshold), (30, 91.5, 0.999, 0.5))
        self.assertEqual(b.reason, "connection_refused")
        self.assertIsNone(b.last_latency)
        self.assertEqual(a.buckets[-1], (math.inf, 100))

    def test_region_label_is_reported(self):
        s = snap.parse_agent_metrics(agent_text(region="lab-b"))
        self.assertEqual(s.region, "lab-b")
        self.assertEqual(s.targets[A].up, 1)

    def test_label_values_with_braces_and_commas(self):
        odd = 'https://odd.example/a,b}c?q="x"'
        escaped = odd.replace('"', '\\"')
        s = snap.parse_agent_metrics(f'# TYPE ping_up gauge\nping_up{{target="{escaped}"}} 1\n')
        self.assertEqual(s.targets[odd].up, 1)

    def test_malformed_text_raises(self):
        with self.assertRaises(snap.MetricsFormatError):
            snap.parse_agent_metrics('ping_up{target="x" 1\n')


class TestPercentiles(unittest.TestCase):
    BUCKETS = [(0.025, 10), (0.05, 60), (0.1, 90), (0.25, 95), (0.5, 98), (1.0, 100), (math.inf, 100)]

    def test_interpolates_within_buckets(self):
        self.assertAlmostEqual(snap.percentile_from_histogram(self.BUCKETS, 0.50), 0.025 + (40 / 50) * 0.025)
        self.assertAlmostEqual(snap.percentile_from_histogram(self.BUCKETS, 0.95), 0.25)

    def test_empty_and_zero(self):
        self.assertIsNone(snap.percentile_from_histogram([], 0.5))
        self.assertIsNone(snap.percentile_from_histogram([(0.1, 0), (math.inf, 0)], 0.5))

    def test_merge_buckets_across_regions(self):
        merged = snap.merge_buckets([[(0.1, 2), (math.inf, 3)], [(0.1, 1), (math.inf, 4)]])
        self.assertEqual(merged, [(0.1, 3), (math.inf, 7)])


# ---------------------------------------------------------------- semantics


class TestTargetStatus(unittest.TestCase):
    def test_up_target(self):
        row = snap.target_status(A, snapshots(agent_text()), NOW)
        self.assertEqual((row["state"], row["up"], row["reason"], row["stale"], row["slow"]), ("up", True, "ok", False, False))
        self.assertEqual((row["name"], row["kind"]), ("Alpha", "http"))
        self.assertEqual(row["last_checked_seconds_ago"], 12)
        self.assertEqual(row["state_since"], snap.iso(NOW - 3600))
        self.assertEqual(row["availability"], 90.0)
        self.assertEqual(row["connection_ms"], {"dns": 3.0, "tcp": 11.0, "tls": 19.0})
        self.assertEqual(row["ssl_cert_expiry_days"], 57.9)
        self.assertEqual((row["interval_seconds"], row["slo_objective"], row["slow_threshold_ms"]), (30, 99.9, 500))
        self.assertEqual((row["regions_total"], row["regions_failing"]), (1, 0))
        self.assertEqual(row["regions"][0]["region"], "default")

    def test_down_target_explains_itself(self):
        row = snap.target_status(B, snapshots(agent_text()), NOW)
        self.assertEqual((row["state"], row["reason"], row["name"]), ("down", "connection_refused", "b.example"))
        self.assertIsNone(row["latency_ms"])
        self.assertEqual(row["state_since"], snap.iso(NOW - 240))

    def test_slow_target(self):
        row = snap.target_status(A, snapshots(agent_text(a_slow=1)), NOW)
        self.assertEqual((row["state"], row["up"], row["slow"]), ("up", True, True))

    def test_stale_data_is_unknown_not_up(self):
        row = snap.target_status(A, snapshots(agent_text(now=NOW - 600)), NOW)
        self.assertEqual((row["state"], row["up"], row["stale"], row["reason"]), ("unknown", False, True, "ok"))
        self.assertIsNone(row["state_since"])

    def test_not_yet_checked_is_pending_not_down(self):
        text = f'# TYPE ping_target_info gauge\nping_target_info{{target="{A}",name="",kind="http"}} 1\n'
        row = snap.target_status(A, snapshots(text), NOW)
        self.assertEqual((row["state"], row["reason"], row["availability"], row["total_checks"]), ("unknown", "pending", None, 0))

    def test_url_no_agent_checks(self):
        row = snap.target_status("https://elsewhere.example", snapshots(agent_text()), NOW)
        self.assertEqual((row["state"], row["reason"], row["regions"]), ("unknown", "not_monitored", []))

    def test_older_agent_without_per_target_settings(self):
        row = snap.target_status(A, snapshots(agent_text(per_target=False, now=NOW - 600)), NOW)
        self.assertEqual((row["state"], row["stale"]), ("unknown", True))  # the agent-wide budget still applies
        bare = f'# TYPE ping_up gauge\nping_up{{target="{A}"}} 1\n'
        row = snap.target_status(A, snapshots(bare), NOW)
        self.assertEqual((row["state"], row["stale"], row["last_checked_at"], row["kind"]), ("up", False, None, "http"))

    def test_kind_and_name_inference(self):
        self.assertEqual(snap.infer_kind("tcp://db:5432"), "tcp")
        self.assertEqual(snap.infer_kind("tls://example.com:443"), "tcp")
        self.assertEqual(snap.infer_kind("dns://example.com/A"), "dns")
        self.assertEqual(snap.display_name("https://api.example.com/health/"), "api.example.com/health")
        self.assertEqual(snap.display_name("dns://example.com/MX"), "example.com/MX")


class TestRegionQuorum(unittest.TestCase):
    def test_one_of_two_regions_failing_is_still_up(self):
        snaps = snapshots(agent_text(region="lab-a"), agent_text(region="lab-b", a_up=0))
        row = snap.target_status(A, snaps, NOW)
        self.assertEqual((row["state"], row["regions_total"], row["regions_failing"]), ("up", 2, 1))
        self.assertEqual({r["region"]: r["state"] for r in row["regions"]}, {"lab-a": "up", "lab-b": "down"})
        self.assertEqual(row["reason"], "ok")
        self.assertEqual((row["success"], row["failures"]), (180, 20))  # counters add up across regions

    def test_majority_down_is_down_with_the_common_reason(self):
        snaps = snapshots(agent_text(region="lab-a"), agent_text(region="lab-b", a_up=0), agent_text(region="lab-c", a_up=0))
        row = snap.target_status(A, snaps, NOW)
        self.assertEqual((row["state"], row["reason"], row["regions_failing"]), ("down", "timeout", 2))

    def test_down_details_come_from_a_region_with_the_common_reason(self):
        snaps = snapshots(
            region_text("lab-a", reason="timeout"),
            region_text("lab-b", reason="http_5xx", status=503),
            region_text("lab-c", reason="http_5xx", status=503),
        )
        row = snap.target_status(A, snaps, NOW)
        self.assertEqual((row["state"], row["reason"], row["http_status"]), ("down", "http_5xx", 503))

    def test_tied_reasons_go_to_the_first_agent(self):
        snaps = snapshots(region_text("lab-a", reason="timeout"), region_text("lab-b", reason="http_5xx", status=503))
        row = snap.target_status(A, snaps, NOW)
        self.assertEqual((row["reason"], row["http_status"]), ("timeout", None))

    def test_the_first_agent_that_names_a_target_names_it_everywhere(self):
        snaps = snapshots(region_text("lab-a", up=1, reason="ok", name=""), region_text("lab-b", up=1, reason="ok", name="Alpha"))
        self.assertEqual(snap.target_names(snaps), {A: "Alpha"})
        self.assertEqual(snap.target_status(A, snaps, NOW)["name"], "Alpha")

    def test_stale_regions_do_not_vote(self):
        snaps = snapshots(agent_text(region="lab-a", a_up=0), agent_text(region="lab-b", now=NOW - 600))
        row = snap.target_status(A, snaps, NOW)
        self.assertEqual((row["state"], row["regions_total"]), ("down", 1))

    def test_slow_needs_a_majority_of_reachable_regions(self):
        snaps = snapshots(agent_text(region="lab-a", a_slow=1), agent_text(region="lab-b"))
        self.assertFalse(snap.target_status(A, snaps, NOW)["slow"])
        snaps = snapshots(agent_text(region="lab-a", a_slow=1), agent_text(region="lab-b", a_slow=1))
        self.assertTrue(snap.target_status(A, snaps, NOW)["slow"])


class TestOverallStatus(unittest.TestCase):
    def row(self, state, slow=False, maintenance=None):
        return {"state": state, "slow": slow, "maintenance": maintenance}

    def test_rules(self):
        cases = [
            ([self.row("up"), self.row("up")], "operational"),
            ([self.row("up"), self.row("down")], "degraded"),
            ([self.row("down"), self.row("down")], "outage"),
            ([self.row("down"), self.row("unknown")], "degraded"),
            ([self.row("up"), self.row("unknown")], "unknown"),
            ([self.row("up"), self.row("up", slow=True)], "degraded"),
            ([], "unknown"),
        ]
        for rows, want in cases:
            self.assertEqual(snap.overall_status(rows), want, rows)

    def test_maintenance_is_set_aside(self):
        active = {"active": True}
        rows = [self.row("up"), self.row("down", maintenance=active)]
        self.assertEqual(snap.overall_status(rows), "operational")
        self.assertEqual((snap.summarize(rows)["maintenance"], snap.summarize(rows)["down"]), (1, 0))
        self.assertEqual(snap.overall_status([self.row("down", maintenance=active)]), "maintenance")
        self.assertEqual(snap.overall_status([self.row("down", maintenance={"active": False})]), "outage")


class TestMaintenanceWindows(unittest.TestCase):
    def test_only_marked_active_or_pending_silences_count(self):
        silences = [
            silence(A),
            silence(B, state="pending"),
            silence(A, comment="muting a noisy alert"),
            silence(A, state="expired"),
            {**silence(A), "matchers": [{"name": "alertname", "value": "TargetSlow", "isRegex": False, "isEqual": True}]},
        ]
        windows = mw.windows_from_silences(silences)
        self.assertEqual([(w["active"], w["comment"]) for w in windows], [(True, "Database upgrade"), (False, "Database upgrade")])
        self.assertNotIn("alice", str(windows))  # creators are never published

    def test_matching(self):
        windows = mw.windows_from_silences([silence(r"https://.*\.example", regex=True)])
        self.assertIsNotNone(mw.maintenance_for(A, windows))
        self.assertIsNone(mw.maintenance_for("https://a.example.org", windows))  # regexes are anchored
        negated = mw.windows_from_silences([silence(A, equal=False)])
        self.assertIsNone(mw.maintenance_for(A, negated))
        self.assertIsNotNone(mw.maintenance_for(B, negated))

    def test_active_window_wins_over_upcoming(self):
        windows = mw.windows_from_silences([silence(A, state="pending"), silence(A)])
        self.assertTrue(mw.maintenance_for(A, windows)["active"])


# ---------------------------------------------------------------- Prometheus helpers


class TestDurations(unittest.TestCase):
    def test_valid(self):
        for text, seconds in [("30s", 30), ("5m", 300), ("24h", 86400), ("7d", 604800), ("2w", 1209600)]:
            self.assertEqual(promql.parse_duration(text), seconds)

    def test_invalid(self):
        for text in ["abc", "5mm", "1", "m5", "", "5m; drop table", "5m\n1h", "5m\n", "0m", "-5m"]:
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
            points = [(ts, pattern[i]) for i, ts in enumerate(range(int(start), int(end) + 1, step)) if i in pattern]
            return matrix(A, points)

        return promql.find_outages(FakeAPI(ranged), [A], window, now)

    def test_uses_the_quorum_series_with_a_fallback(self):
        api = FakeAPI()
        promql.find_outages(api, [A], 600, NOW)
        query = api.calls[0][0]
        self.assertTrue(query.startswith("min_over_time(iyup:target_up:quorum[15s]) or ("), query)
        # Raw data stands in only where the rules have produced nothing lately,
        # and votes by majority too: a step that merely missed a quorum sample
        # must not turn a one-region failure into an outage.
        self.assertIn("* 2 >= bool count by (target) (min_over_time(ping_up[15s]))", query)
        self.assertTrue(query.endswith("unless on (target) count_over_time(iyup:target_up:quorum[300s]))"), query)

    def test_single_closed_outage(self):
        data = self.run_outages({i: (0 if 10 <= i <= 13 else 1) for i in range(40)})
        self.assertEqual(len(data["outages"]), 1)
        outage = data["outages"][0]
        self.assertEqual((outage["ongoing"], outage["duration_seconds"], outage["started_before_window"]), (False, 60, False))

    def test_ongoing_and_window_edge(self):
        pattern = {i: 1 for i in range(40)}
        pattern.update({0: 0, 1: 0, 38: 0, 39: 0})
        ongoing, earliest = self.run_outages(pattern)["outages"]
        self.assertEqual((ongoing["ongoing"], ongoing["ended_at"]), (True, None))
        self.assertTrue(earliest["started_before_window"])

    def test_data_gap_ends_an_outage_without_inventing_recovery(self):
        pattern = {i: 1 for i in range(40)}
        pattern.update({5: 0, 6: 0})
        del pattern[7]
        data = self.run_outages(pattern)
        self.assertEqual([o["duration_seconds"] for o in data["outages"]], [30])

    def test_agent_restart_does_not_split_an_outage(self):
        pattern = {i: 1 for i in range(40)}
        pattern.update({i: 0 for i in range(10, 30)})
        del pattern[18], pattern[19]
        self.assertEqual([o["duration_seconds"] for o in self.run_outages(pattern)["outages"]], [20 * 15])

    def test_long_silence_splits_an_outage(self):
        pattern = {i: 0 for i in range(40)}
        for i in range(10, 50):
            pattern.pop(i, None)
        pattern.update({i: 0 for i in range(50, 60)})
        self.assertEqual(len(self.run_outages(pattern, window=900)["outages"]), 2)

    def test_long_windows_stay_under_the_point_limit(self):
        api = FakeAPI()
        data = promql.find_outages(api, [A], 30 * 86400, NOW)
        _, start, end, step = api.calls[0]
        self.assertLessEqual((end - start) / step + 1, promql.MAX_POINTS + 1)
        self.assertEqual(data["resolution_seconds"], step)


class TestAvailabilityHistory(unittest.TestCase):
    def test_buckets_are_aligned_and_gaps_stay_null(self):
        def ranged(query, start, end, step):
            ts = [int(start) + i * step for i in range(4)]
            return matrix(A, [(ts[0], 1), (ts[2], 0.5), (ts[3], 1)])

        def instant(query):
            return vector({A: 0.2125}) if "histogram_quantile" in query else vector({A: 0.875})

        api = FakeAPI(ranged, instant)
        data = promql.availability_history(api, [A, B], 4 * 3600, 4, NOW)
        self.assertIn("iyup:target_up:quorum", api.calls[0][0])
        self.assertIn("unless on (target) count_over_time(iyup:target_up:quorum[3600s])", api.calls[0][0])
        self.assertEqual((data["bucket_seconds"], data["end"] % 3600, data["end"] - data["start"]), (3600, 0, 4 * 3600))
        row_a, row_b = data["targets"]
        self.assertEqual(row_a["buckets"], [100.0, None, 50.0, 100.0])
        self.assertEqual((row_a["availability"], row_a["latency_p95_ms"]), (87.5, 212.5))
        self.assertEqual(row_b["buckets"], [None] * 4)


def slo_instant(query):
    if "objective" in query:
        return vector({A: 0.999, B: 0.99})
    if query.startswith("avg_over_time(iyup:target_availability:avg5m"):
        return vector({A: 0.9995, B: 0.9})
    if query.startswith("count_over_time"):
        return vector({A: 86400 * 30 / 15, B: 86400 * 2 / 15})  # A: the whole window, B: two days of data
    if "rate1h" in query:
        return vector({A: 0.0144})
    if "rate6h" in query:
        return vector({A: 0.0005})
    return []


class TestSLOReport(unittest.TestCase):
    def test_budget_math(self):
        a, b = promql.slo_report(FakeAPI(instant=slo_instant), [A, B], 30 * 86400, NOW)["targets"]
        # 99.9% of 30 days allows 43m12s; 0.05% of 30 days down is 21m36s: half the budget left.
        self.assertEqual((a["objective"], a["availability"]), (99.9, 99.95))
        self.assertEqual((a["allowed_downtime_seconds"], a["downtime_seconds"], a["budget_remaining"]), (2592, 1296, 50.0))
        self.assertEqual((a["burn_rate_1h"], a["burn_rate_6h"]), (14.4, 0.5))
        # Only two days of data for B: downtime counts what was seen; the budget is the whole window's.
        self.assertEqual((b["coverage_seconds"], b["downtime_seconds"], b["allowed_downtime_seconds"]), (2 * 86400, 17280, 25920))
        self.assertAlmostEqual(b["budget_remaining"], 33.33, places=2)

    def test_overspent_budget_goes_negative(self):
        def instant(query):
            if "objective" in query:
                return vector({A: 0.999})
            if query.startswith("avg_over_time"):
                return vector({A: 0.99})
            if query.startswith("count_over_time"):
                return vector({A: 86400 * 30 / 15})
            return []

        row = promql.slo_report(FakeAPI(instant=instant), [A], 30 * 86400, NOW)["targets"][0]
        self.assertEqual(row["budget_remaining"], -900.0)


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
        self.assertEqual(body["summary"], {"up": 1, "down": 1, "unknown": 0, "slow": 0, "maintenance": 0, "total": 2})
        self.assertEqual(
            body["monitor"],
            {"interval_seconds": 30, "agents": [{"region": "default", "reachable": True}], "alertmanager": "reachable"},
        )
        self.assertEqual([t["name"] for t in body["targets"]], ["Alpha", "b.example"])  # ordered by name
        self.assertEqual(body["targets"][1]["reason"], "connection_refused")
        self.assertEqual(body["maintenance"], [])

    def test_status_at_startup_is_unknown_not_degraded(self):
        text = f'# TYPE ping_target_info gauge\nping_target_info{{target="{A}",name="",kind="http"}} 1\n'
        self.upstreams(agents={AGENT_1: text})
        body = self.client.get("/status").json()
        self.assertEqual((body["status"], body["targets"][0]["reason"]), ("unknown", "pending"))

    def test_regions_vote(self):
        self.upstreams(agents={AGENT_1: agent_text(region="lab-a"), AGENT_2: agent_text(region="lab-b", a_up=0)})
        body = self.client.get("/status").json()
        alpha = body["targets"][0]
        self.assertEqual((alpha["state"], alpha["regions_total"], alpha["regions_failing"]), ("up", 2, 1))
        self.assertEqual([a["region"] for a in body["monitor"]["agents"]], ["lab-a", "lab-b"])

    def test_one_agent_down_still_answers(self):
        self.upstreams(agents={AGENT_1: agent_text(region="lab-a"), AGENT_2: None})
        resp = self.client.get("/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([a["reachable"] for a in resp.json()["monitor"]["agents"]], [True, False])

    def test_status_when_every_agent_is_unreachable(self):
        self.upstreams(agents={AGENT_1: None, AGENT_2: None})
        resp = self.client.get("/status")
        self.assertEqual((resp.status_code, resp.json()["detail"]), (502, "ping-agent is unreachable (connection failed)"))

    def test_maintenance_sets_a_target_aside(self):
        self.upstreams(silences=[silence(B), silence(A, state="pending", comment="[maintenance] Certificate rotation")])
        body = self.client.get("/status").json()
        self.assertEqual((body["status"], body["summary"]["maintenance"]), ("operational", 1))  # B is down, but in maintenance
        b = next(t for t in body["targets"] if t["url"] == B)
        self.assertEqual((b["state"], b["maintenance"]["active"], b["maintenance"]["comment"]), ("down", True, "Database upgrade"))
        self.assertEqual([(w["active"], w["targets"]) for w in body["maintenance"]], [(True, ["b.example"]), (False, ["Alpha"])])
        self.assertNotIn("alice", str(body))

    def test_status_survives_alertmanager_outage(self):
        self.upstreams(am_down=True)
        body = self.client.get("/status").json()
        self.assertEqual((body["monitor"]["alertmanager"], body["status"]), ("unreachable", "degraded"))

    def test_configured_targets_override_discovery(self):
        main.CONFIGURED_TARGETS = [B, "https://new.example"]
        self.upstreams()
        body = self.client.get("/status").json()
        self.assertEqual([t["url"] for t in body["targets"]], [B, "https://new.example"])
        self.assertEqual(body["targets"][1]["reason"], "not_monitored")
        self.assertEqual(self.client.get("/targets").json()["source"], "config")

    def test_configured_targets_keep_the_agents_names(self):
        main.CONFIGURED_TARGETS = [A, B]
        self.upstreams(ranged=lambda q, s, e, st: matrix(A, [(int(s), 1)]))
        self.assertEqual(self.client.get("/status").json()["targets"][0]["name"], "Alpha")
        self.assertEqual(self.client.get("/targets").json()["targets"][0]["name"], "Alpha")
        self.assertEqual(self.client.get("/history?window=1h&buckets=4").json()["targets"][0]["name"], "Alpha")

    def test_targets_discovered_from_agent(self):
        self.upstreams()
        body = self.client.get("/targets").json()
        self.assertEqual(body, {"source": "agent", "targets": [{"url": A, "name": "Alpha"}, {"url": B, "name": "b.example"}]})

    def test_target_detail(self):
        self.upstreams()
        resp = self.client.get("/targets/https%3A%2F%2Fa.example")
        self.assertEqual((resp.status_code, resp.json()["success"], resp.json()["failures"]), (200, 90, 10))
        self.assertEqual(self.client.get("/targets/https%3A%2F%2Fnope.example").status_code, 404)

    def test_target_ids_are_decoded_once(self):
        # uvicorn decodes the request path once before routing, so the handler
        # gets "%20" for "%2520". (TestClient decodes twice, hence the direct call.)
        target = "https://a.example/a%20b"
        self.upstreams(agents={AGENT_1: region_text("lab-a", target=target, up=1, reason="ok")})
        self.assertEqual(main.target_detail(target)["url"], target)

    def test_uptime_summary_adds_regions(self):
        self.upstreams(agents={AGENT_1: agent_text(region="lab-a"), AGENT_2: agent_text(region="lab-b")})
        rows = {r["url"]: r for r in self.client.get("/uptime-summary").json()["targets"]}
        self.assertEqual((rows[A]["success"], rows[A]["availability"]), (180, "90.00%"))

    def test_windowed_uptime(self):
        self.upstreams(instant=lambda q: vector({A: 3, B: 2}) if "ping_success_total" in q else vector({A: 1, B: 2}))
        body = self.client.get("/uptime-summary-windowed?window=5m").json()
        self.assertEqual([r["availability"] for r in body["targets"]], ["75.00%", "50.00%"])

    def test_windowed_uptime_errors(self):
        self.upstreams(prom_down=True)
        self.assertEqual(self.client.get("/uptime-summary-windowed?window=0m").status_code, 400)
        resp = self.client.get("/uptime-summary-windowed?window=5m")
        self.assertEqual((resp.status_code, resp.json()["detail"]), (502, "Prometheus is unreachable (connection failed)"))
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
        self.assertEqual((body["bucket_seconds"], len(body["bucket_starts"])), (1800, 48))
        row_a, row_b = body["targets"]
        self.assertEqual((row_a["name"], row_a["availability"], row_a["buckets"]), ("Alpha", 100.0, [100.0] * 48))
        self.assertEqual(row_b["buckets"], [None] * 48)
        sent = len(fake.queries)
        self.client.get("/history?window=24h&buckets=48")
        self.assertEqual(len(fake.queries), sent)  # served from the cache

    def test_validation(self):
        self.upstreams()
        self.assertEqual(self.client.get("/history?window=10m&buckets=200").status_code, 400)
        self.assertEqual(self.client.get("/history?window=60d").status_code, 400)
        self.assertEqual(self.client.get("/history?buckets=0").status_code, 422)
        self.assertEqual(self.client.get("/outages?window=30s").status_code, 400)
        self.assertEqual(self.client.get("/slo?window=10m").status_code, 400)
        self.assertEqual(self.client.get("/slo?window=120d").status_code, 400)

    def test_history_falls_back_to_prometheus_targets_when_agents_are_down(self):
        self.upstreams(agents={AGENT_1: None}, ranged=lambda q, s, e, st: matrix(B, [(int(s), 1)]))
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
        self.assertEqual((body["total"], body["truncated"], len(body["outages"])), (3, True, 2))
        self.assertEqual((body["by_target"][0]["count"], body["by_target"][0]["name"]), (3, "Alpha"))

    def test_slo(self):
        self.upstreams(instant=slo_instant)
        body = self.client.get("/slo").json()
        alpha = body["targets"][0]
        self.assertEqual((body["window"], alpha["name"], alpha["budget_remaining"]), ("30d", "Alpha", 50.0))

    def test_request_metrics_use_route_templates(self):
        self.upstreams()
        self.client.get("/targets/https%3A%2F%2Fa.example")
        self.client.get("/wp-admin/setup.php")
        self.client.get("/assets/status.css")
        self.client.get("/openapi.json")
        self.client.post("/status")
        exposition = self.client.get("/metrics").text
        for expected in [
            'path="/targets/{target_url:path}"',
            'path="unmatched",status="404"',
            'path="/assets",status="200"',
            'path="/openapi.json",status="200"',
            'path="/status",status="405"',
        ]:
            self.assertIn(expected, exposition)
        self.assertNotIn('path="unmatched",status="200"', exposition)
        self.assertNotIn("a.example", exposition)

    def test_status_page(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("script-src 'self'", resp.headers["content-security-policy"])
        js = self.client.get("/assets/status.js")
        self.assertEqual((js.status_code, js.headers["cache-control"]), (200, "no-cache"))
        self.assertIn("etag", js.headers)

    def test_openapi_documents_the_contract(self):
        schema = self.client.get("/openapi.json").json()
        for path in ["/status", "/history", "/outages", "/slo", "/targets/{target_url}"]:
            self.assertIn(path, schema["paths"])
        for model in ["TargetStatus", "RegionStatus", "Maintenance", "SLOTarget"]:
            self.assertIn(model, schema["components"]["schemas"])


class TestAgentsOverRealHTTP(GatewayState):
    """End to end over sockets: two live agents, proving the parallel fetch,
    timeouts and parsing work against real servers, not just mocks."""

    def test_status_against_live_metrics_servers(self):
        now = time.time()
        bodies = [agent_text(now=now, region="lab-a").encode(), agent_text(now=now, region="lab-b", a_up=0).encode()]
        servers = []
        for body in bodies:
            handler = type(
                "Handler",
                (http.server.BaseHTTPRequestHandler,),
                {
                    "payload": body,
                    "do_GET": lambda self: (
                        self.send_response(200),
                        self.send_header("Content-Length", str(len(self.payload))),
                        self.end_headers(),
                        self.wfile.write(self.payload),
                    ),
                    "log_message": lambda self, *args: None,
                },
            )
            try:
                server = http.server.HTTPServer(("127.0.0.1", 0), handler)
            except OSError as exc:
                self.skipTest(f"cannot bind a local test server: {exc}")
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
        main.ALERTMANAGER_URL = ""
        main.PING_AGENT_METRICS_URLS = [f"http://127.0.0.1:{s.server_port}/metrics" for s in servers]
        try:
            payload = main.status()
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
        alpha = payload["targets"][0]
        self.assertEqual((alpha["state"], alpha["regions_failing"]), ("up", 1))
        self.assertEqual(payload["monitor"]["alertmanager"], "not_configured")


if __name__ == "__main__":
    unittest.main()
