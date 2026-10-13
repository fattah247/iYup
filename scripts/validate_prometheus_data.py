#!/usr/bin/env python3
"""
Validate Prometheus data for correctness and identify issues.
This script checks the actual data that Grafana queries and reports any problems.

Usage:
    python3 scripts/validate_prometheus_data.py [prometheus_url]
"""

import sys
import json
import requests
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta

PROMETHEUS_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9090"

ISSUES = []
WARNINGS = []


def add_issue(severity: str, message: str, details: Optional[Dict] = None):
    """Add an issue or warning."""
    entry = {
        "severity": severity,
        "message": message,
        "timestamp": datetime.now().isoformat(),
        "details": details or {}
    }
    if severity == "ERROR":
        ISSUES.append(entry)
    else:
        WARNINGS.append(entry)
    print(f"{'❌' if severity == 'ERROR' else '⚠️ '} [{severity}] {message}")


def query_prometheus(query: str) -> Optional[Dict]:
    """Query Prometheus and return the result."""
    try:
        response = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        
        if data.get("status") != "success":
            error = data.get("error", {}).get("message", "Unknown error")
            add_issue("ERROR", f"Query failed: {query}", {"error": error})
            return None
        
        return data.get("data", {})
    except requests.exceptions.RequestException as e:
        add_issue("ERROR", f"Cannot reach Prometheus: {e}")
        return None


def check_targets_health() -> bool:
    """Check if Prometheus targets are healthy."""
    print("\n" + "="*80)
    print("🎯 Checking Prometheus Targets Health")
    print("="*80)
    
    try:
        response = requests.get(f"{PROMETHEUS_URL}/api/v1/targets", timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("status") != "success":
            add_issue("ERROR", "Failed to get targets status")
            return False
        
        targets = data.get("data", {}).get("activeTargets", [])
        all_up = True
        
        for t in targets:
            labels = t.get("labels", {})
            job = labels.get("job", "unknown")
            health = t.get("health", "unknown")
            last_error = t.get("lastError", "")
            last_scrape = t.get("lastScrape", "")
            
            if health != "up":
                add_issue("ERROR", f"Target {job} is {health}", {
                    "job": job,
                    "health": health,
                    "last_error": last_error,
                    "last_scrape": last_scrape
                })
                all_up = False
            else:
                print(f"✅ {job}: UP")
                if last_error:
                    add_issue("WARNING", f"Target {job} has last error (but is UP)", {
                        "job": job,
                        "last_error": last_error
                    })
        
        if len(targets) == 0:
            add_issue("ERROR", "No active targets found")
            return False
        
        return all_up
    except requests.exceptions.RequestException as e:
        add_issue("ERROR", f"Cannot check targets: {e}")
        return False


def validate_counter_consistency(metric_name: str, expected_targets: List[str]) -> bool:
    """Validate that counters exist and are consistent."""
    print(f"\n📊 Validating {metric_name}")
    
    data = query_prometheus(metric_name)
    if not data:
        return False
    
    results = data.get("result", [])
    if len(results) == 0:
        add_issue("ERROR", f"No data for {metric_name} - metric may not exist")
        return False
    
    found_targets = set()
    for r in results:
        metric = r.get("metric", {})
        target = metric.get("target", "")
        value = r.get("value", [])
        
        if len(value) < 2:
            add_issue("ERROR", f"Invalid value format for {metric_name} with target {target}")
            continue
        
        try:
            val = float(value[1])
            if val < 0:
                add_issue("ERROR", f"Negative counter value for {metric_name} target={target}: {val}")
            found_targets.add(target)
            print(f"  ✅ {target}: {val}")
        except (TypeError, ValueError):
            add_issue("ERROR", f"Invalid counter value for {metric_name} target={target}: {value[1]}")
    
    # Check if all expected targets are present
    missing = set(expected_targets) - found_targets
    if missing:
        add_issue("WARNING", f"Missing targets in {metric_name}: {missing}")
    
    return True


def validate_success_failure_consistency(expected_targets: List[str]) -> bool:
    """Validate that success + failure counters are consistent."""
    print("\n📊 Validating Success/Failure Consistency")
    
    success_data = query_prometheus("ping_success_total")
    failure_data = query_prometheus("ping_failure_total")
    
    if not success_data or not failure_data:
        return False
    
    success_by_target = {}
    for r in success_data.get("result", []):
        metric = r.get("metric", {})
        target = metric.get("target", "")
        value = r.get("value", [])
        if len(value) >= 2:
            try:
                # One series per region: a target's checks are the sum.
                success_by_target[target] = success_by_target.get(target, 0.0) + float(value[1])
            except (TypeError, ValueError):
                pass
    
    failure_by_target = {}
    for r in failure_data.get("result", []):
        metric = r.get("metric", {})
        target = metric.get("target", "")
        value = r.get("value", [])
        if len(value) >= 2:
            try:
                failure_by_target[target] = failure_by_target.get(target, 0.0) + float(value[1])
            except (TypeError, ValueError):
                pass
    
    all_consistent = True
    for target in expected_targets:
        success = success_by_target.get(target, 0.0)
        failure = failure_by_target.get(target, 0.0)
        total = success + failure
        
        if total == 0:
            add_issue("WARNING", f"No pings recorded for {target} (success=0, failure=0)")
        else:
            availability = (success / total) * 100
            print(f"  ✅ {target}: {success} success, {failure} failure, {availability:.1f}% availability")
            
            # Check for suspicious patterns
            if failure > success * 10 and total > 10:
                add_issue("WARNING", f"High failure rate for {target}: {failure}/{total} ({100 - availability:.1f}% failures)")
    
    return all_consistent


def validate_availability_calculation(expected_targets: List[str]) -> bool:
    """Validate availability percentage calculations match Grafana queries."""
    print("\n📊 Validating Availability Calculations")
    
    for target in expected_targets:
        # Query the same calculation Grafana uses
        query = f'100 * increase(ping_success_total{{target="{target}"}}[5m]) / clamp_min(increase(ping_success_total{{target="{target}"}}[5m]) + increase(ping_failure_total{{target="{target}"}}[5m]), 1)'
        
        data = query_prometheus(query)
        if not data:
            continue
        
        results = data.get("result", [])
        if len(results) == 0:
            add_issue("WARNING", f"No availability data for {target} in last 5m")
            continue
        
        for r in results:
            value = r.get("value", [])
            if len(value) >= 2:
                try:
                    availability = float(value[1])
                    if availability < -1e-9 or availability > 100 + 1e-9:  # allow float rounding
                        add_issue("ERROR", f"Invalid availability for {target}: {availability}% (should be 0-100)")
                    else:
                        print(f"  ✅ {target}: {availability:.1f}% availability (5m)")
                except (TypeError, ValueError):
                    add_issue("ERROR", f"Invalid availability value for {target}")
    
    return True


def validate_latency_metrics(expected_targets: List[str]) -> bool:
    """Validate latency histogram metrics."""
    print("\n📊 Validating Latency Metrics")
    
    for target in expected_targets:
        # Check histogram buckets exist
        query = f'ping_latency_seconds_bucket{{target="{target}"}}'
        data = query_prometheus(query)
        
        if not data:
            continue
        
        results = data.get("result", [])
        if len(results) == 0:
            # Latency is recorded only when a response arrived, so a target
            # that has never answered (connection refused, timeouts) has none.
            up = query_prometheus(f'max(ping_up{{target="{target}"}})')
            never_answered = up and up.get("result") and float(up["result"][0]["value"][1]) == 0
            if never_answered:
                print(f"  ➖ {target}: down without a response, so no latency (expected)")
            else:
                add_issue("WARNING", f"No latency histogram data for {target}")
            continue
        
        # Check average latency calculation
        avg_query = f'sum(rate(ping_latency_seconds_sum{{target="{target}"}}[1m])) / clamp_min(sum(rate(ping_latency_seconds_count{{target="{target}"}}[1m])), 1)'
        avg_data = query_prometheus(avg_query)
        
        if avg_data:
            avg_results = avg_data.get("result", [])
            if len(avg_results) > 0:
                for r in avg_results:
                    value = r.get("value", [])
                    if len(value) >= 2:
                        try:
                            avg_latency = float(value[1])
                            if avg_latency < 0:
                                add_issue("ERROR", f"Negative average latency for {target}: {avg_latency}s")
                            elif avg_latency > 60:
                                add_issue("WARNING", f"Very high average latency for {target}: {avg_latency}s")
                            else:
                                print(f"  ✅ {target}: avg latency {avg_latency*1000:.1f}ms")
                        except (TypeError, ValueError):
                            pass
    
    return True


def validate_api_gateway_metrics() -> bool:
    """Validate API Gateway metrics."""
    print("\n📊 Validating API Gateway Metrics")
    
    # Check requests counter exists
    data = query_prometheus("api_gateway_requests_total")
    if not data:
        add_issue("WARNING", "No API Gateway request metrics found")
        return False
    
    results = data.get("result", [])
    if len(results) == 0:
        add_issue("WARNING", "API Gateway has no recorded requests yet")
        return True
    
    # Check for error rates
    error_query = 'sum(rate(api_gateway_requests_total{status=~"5.."}[5m]))'
    error_data = query_prometheus(error_query)
    
    if error_data:
        error_results = error_data.get("result", [])
        if len(error_results) > 0:
            for r in error_results:
                value = r.get("value", [])
                if len(value) >= 2:
                    try:
                        error_rate = float(value[1])
                        if error_rate > 0.1:  # More than 0.1 errors per second
                            add_issue("WARNING", f"High API Gateway error rate: {error_rate:.3f} errors/sec")
                        else:
                            print(f"  ✅ API Gateway error rate: {error_rate:.3f} errors/sec")
                    except (TypeError, ValueError):
                        pass
    
    return True


def main():
    """Main validation routine."""
    print("🔍 Validating Prometheus Data Quality")
    print(f"Prometheus URL: {PROMETHEUS_URL}")
    print(f"Time: {datetime.now().isoformat()}")
    
    # Check if Prometheus is reachable
    try:
        response = requests.get(f"{PROMETHEUS_URL}/api/v1/status/config", timeout=5)
        response.raise_for_status()
        print("✅ Prometheus is reachable")
    except requests.exceptions.RequestException:
        add_issue("ERROR", f"Cannot reach Prometheus at {PROMETHEUS_URL}")
        print("\n" + "="*80)
        print("❌ VALIDATION FAILED - Cannot connect to Prometheus")
        print("="*80)
        print("\nMake sure Prometheus is running and port-forwarded:")
        print("  kubectl port-forward svc/iyup-prometheus 9090:9090")
        sys.exit(1)
    
    # Expected targets: whatever the ping-agent says it is configured to check.
    info = query_prometheus("ping_target_info")
    expected_targets = sorted(
        {r["metric"]["target"] for r in (info or {}).get("result", []) if r.get("metric", {}).get("target")}
    ) or ["https://google.com", "https://github.com"]
    print(f"Expected targets: {', '.join(expected_targets)}")
    
    # Run all validations
    targets_ok = check_targets_health()
    
    if not targets_ok:
        print("\n⚠️  Some targets are down - continuing validation anyway...")
    
    # Validate counters
    success_ok = validate_counter_consistency("ping_success_total", expected_targets)
    failure_ok = validate_counter_consistency("ping_failure_total", expected_targets)
    
    # Validate consistency
    consistency_ok = validate_success_failure_consistency(expected_targets)
    
    # Validate calculations
    calc_ok = validate_availability_calculation(expected_targets)
    
    # Validate latency
    latency_ok = validate_latency_metrics(expected_targets)
    
    # Validate API Gateway
    api_ok = validate_api_gateway_metrics()
    
    # Summary
    print("\n" + "="*80)
    print("📋 VALIDATION SUMMARY")
    print("="*80)
    
    total_errors = len([i for i in ISSUES if i["severity"] == "ERROR"])
    total_warnings = len([i for i in ISSUES + WARNINGS if i["severity"] == "WARNING"])
    
    if total_errors == 0 and total_warnings == 0:
        print("✅ All validations passed! No issues found.")
        print("\nData quality is good. Grafana should display correct values.")
        return 0
    else:
        print(f"\n❌ Found {total_errors} error(s) and {total_warnings} warning(s)")
        
        if total_errors > 0:
            print("\n🔴 ERRORS:")
            for issue in [i for i in ISSUES if i["severity"] == "ERROR"]:
                print(f"  - {issue['message']}")
                if issue.get("details"):
                    for k, v in issue["details"].items():
                        print(f"    {k}: {v}")
        
        if total_warnings > 0:
            print("\n🟡 WARNINGS:")
            for issue in [i for i in ISSUES + WARNINGS if i["severity"] == "WARNING"]:
                print(f"  - {issue['message']}")
                if issue.get("details"):
                    for k, v in issue["details"].items():
                        print(f"    {k}: {v}")
        
        print("\n💡 Next steps:")
        print("  1. Review the errors above")
        print("  2. Fix any code issues")
        print("  3. Restart services: ./scripts/update_stack.sh")
        print("  4. Run validation again: python3 scripts/validate_prometheus_data.py")
        
        return 1


if __name__ == "__main__":
    sys.exit(main())
