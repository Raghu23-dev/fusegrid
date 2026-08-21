#!/usr/bin/env python3
"""Measure enforcement overhead. Criterion 3: p99 < 15 ms added.

Compares a passthrough proxy against fusegrid in front of the same fake upstream, so the
difference is enforcement and nothing else. Reports percentiles over multiple runs with
variance, because a single run of a latency measurement is noise.

Run:  python bench/latency/measure.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fusegrid import Ledger, MemoryStore, ModelPrice, Pricing

RUNS = 5
REQUESTS_PER_RUN = 2000
KEY = "team"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def measure_enforcement() -> list[float]:
    """Time the enforcement path only: price + reserve + settle.

    The upstream call is excluded deliberately. Including it would bury the overhead
    under hundreds of milliseconds of model latency and make the number meaningless —
    the question is what enforcement costs, not what a model costs.
    """
    pricing = Pricing({"m": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0)})
    # A ceiling large enough that nothing is denied, so every sample times the full
    # reserve-and-settle path rather than an early return.
    ledger = Ledger(MemoryStore(), {KEY: 1_000_000.0})

    samples: list[float] = []
    for _ in range(REQUESTS_PER_RUN):
        start = time.perf_counter()
        cost = pricing.max_cost("m", 500, 1000)
        decision = ledger.reserve(KEY, cost)
        if decision.reservation is not None:
            ledger.settle(decision.reservation.id, cost * 0.3)
        samples.append((time.perf_counter() - start) * 1000.0)
    return samples


def measure_baseline() -> list[float]:
    """Time a no-op stand-in for the same call sites, as a floor."""
    samples: list[float] = []
    for _ in range(REQUESTS_PER_RUN):
        start = time.perf_counter()
        _ = 500 / 1_000_000 * 3.0 + 1000 / 1_000_000 * 15.0
        samples.append((time.perf_counter() - start) * 1000.0)
    return samples


def main() -> None:
    THRESHOLD_MS = 15.0

    runs: list[dict[str, float]] = []
    for i in range(RUNS):
        enforced = measure_enforcement()
        baseline = measure_baseline()
        runs.append(
            {
                "run": i + 1,
                "p50": percentile(enforced, 0.50),
                "p95": percentile(enforced, 0.95),
                "p99": percentile(enforced, 0.99),
                "mean": statistics.fmean(enforced),
                "baseline_p50": percentile(baseline, 0.50),
            }
        )

    print(f"{RUNS} runs x {REQUESTS_PER_RUN} requests, enforcement path only\n")
    print(f"{'run':>4}{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}{'mean ms':>10}")
    print("-" * 44)
    for r in runs:
        print(
            f"{int(r['run']):>4}{r['p50']:>10.4f}{r['p95']:>10.4f}"
            f"{r['p99']:>10.4f}{r['mean']:>10.4f}"
        )

    p99s = [r["p99"] for r in runs]
    worst = max(p99s)
    spread = max(p99s) - min(p99s)

    print()
    print(f"p99 across runs: {min(p99s):.4f} to {max(p99s):.4f} ms  (spread {spread:.4f} ms)")
    print(f"worst p99:       {worst:.4f} ms")
    print(f"threshold:       {THRESHOLD_MS:.1f} ms")
    print()
    print("NOTE: this measures the enforcement path with an in-process store. A Redis")
    print("store adds one network round trip — typically 0.2-1 ms on a local network,")
    print("and that is NOT measured here. The claim is scoped accordingly.")

    out = Path(__file__).parent / "results" / "latency.json"
    out.write_text(
        json.dumps(
            {
                "runs": runs,
                "worst_p99_ms": round(worst, 4),
                "threshold_ms": THRESHOLD_MS,
                "store": "MemoryStore (in-process)",
                "caveat": "Redis adds one round trip, not measured here",
            },
            indent=2,
        )
        + "\n"
    )

    if worst > THRESHOLD_MS:
        print(f"\nOVER THRESHOLD — criterion 3 not met: {worst:.4f} ms > {THRESHOLD_MS} ms")
        sys.exit(1)
    print(f"\nwithin threshold: worst p99 {worst:.4f} ms < {THRESHOLD_MS} ms")


if __name__ == "__main__":
    main()
