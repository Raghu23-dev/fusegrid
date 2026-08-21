#!/usr/bin/env python3
"""Replay the four baseline failures against fusegrid.

`bench/baseline/failopen.py` measured four enforcement patterns and all four failed
open, 95–100% over ceiling. This runs the SAME four scenarios through fusegrid's ledger
and reports whether the ceiling held.

That side-by-side is the headline result. Anything else is commentary.

Run:  python bench/enforce/replay.py
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fusegrid import Ledger, LedgerUnavailable, MemoryStore, ModelPrice, Pricing, UnpricedModel

CEILING = 1.00
COST = 0.05
REQUESTS = 40
KEY = "team"


@dataclass
class Outcome:
    scenario: str
    baseline_allowed: int
    baseline_spend: float
    fusegrid_allowed: int
    fusegrid_spend: float
    held: bool
    note: str


def _ledger() -> tuple[Ledger, MemoryStore]:
    store = MemoryStore()
    return Ledger(store, {KEY: CEILING}), store


def scenario_race() -> Outcome:
    """Baseline: 40 concurrent requests all admitted, $2.00 spent, 100% over."""
    ledger, store = _ledger()
    allowed: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        d = ledger.reserve(KEY, COST)
        with lock:
            allowed.append(d.allowed)

    threads = [threading.Thread(target=attempt) for _ in range(REQUESTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return Outcome(
        scenario="check-then-act race",
        baseline_allowed=40,
        baseline_spend=2.00,
        fusegrid_allowed=sum(allowed),
        fusegrid_spend=store.spent(KEY),
        held=store.spent(KEY) <= CEILING + 1e-9,
        note="reservation is a single atomic operation, so concurrent requests cannot "
        "all observe the same balance",
    )


def scenario_variable_cost() -> Outcome:
    """Baseline: 19 cheap calls then one 20x completion, $1.95 spent, 95% over."""
    ledger, store = _ledger()
    allowed = 0

    for _ in range(19):
        if ledger.reserve(KEY, COST).allowed:
            allowed += 1
    # The expensive call: conventional enforcement admits it because headroom remains
    # and the cost is not yet known. fusegrid reserves the MAXIMUM, so it does not fit.
    if ledger.reserve(KEY, COST * 20).allowed:
        allowed += 1

    return Outcome(
        scenario="post-hoc accounting",
        baseline_allowed=20,
        baseline_spend=1.95,
        fusegrid_allowed=allowed,
        fusegrid_spend=store.spent(KEY),
        held=store.spent(KEY) <= CEILING + 1e-9,
        note="the maximum is reserved before the call, so an expensive request cannot "
        "slip through remaining headroom",
    )


def scenario_store_down() -> Outcome:
    """Baseline: ledger unavailable, 40 calls admitted, ledger recorded $0.00."""
    ledger, store = _ledger()
    store.fail = True
    allowed = 0
    denied = 0

    for _ in range(REQUESTS):
        try:
            if ledger.reserve(KEY, COST).allowed:
                allowed += 1
        except LedgerUnavailable:
            denied += 1

    store.fail = False
    return Outcome(
        scenario="best-effort recording",
        baseline_allowed=40,
        baseline_spend=2.00,
        fusegrid_allowed=allowed,
        fusegrid_spend=store.spent(KEY),
        held=allowed == 0,
        note=f"fails closed: {denied} requests denied while the store was down, so no "
        "unenforced spend was possible",
    )


def scenario_unpriced() -> Outcome:
    """Baseline: unknown model costed at $0.00, 40 calls admitted, 100% over."""
    ledger, store = _ledger()
    pricing = Pricing({"known-model": ModelPrice(1.0, 2.0)})
    allowed = 0
    denied = 0

    for _ in range(REQUESTS):
        try:
            cost = pricing.max_cost("brand-new-model", 1000, 1000)
        except UnpricedModel:
            denied += 1
            continue
        if ledger.reserve(KEY, cost).allowed:
            allowed += 1

    return Outcome(
        scenario="unpriced model",
        baseline_allowed=40,
        baseline_spend=2.00,
        fusegrid_allowed=allowed,
        fusegrid_spend=store.spent(KEY),
        held=allowed == 0,
        note=f"an unknown model has no maximum, so nothing can be reserved: {denied} "
        "denied rather than costed at zero",
    )


def main() -> None:
    outcomes = [
        scenario_race(),
        scenario_variable_cost(),
        scenario_store_down(),
        scenario_unpriced(),
    ]

    print(f"ceiling ${CEILING:.2f} · ${COST:.2f}/call · {REQUESTS} requests\n")
    print(f"{'scenario':<24}{'baseline':>20}{'fusegrid':>20}   ceiling")
    print(f"{'':<24}{'allowed  spend':>20}{'allowed  spend':>20}")
    print("-" * 76)
    for o in outcomes:
        print(
            f"{o.scenario:<24}"
            f"{o.baseline_allowed:>7}  ${o.baseline_spend:>9.2f}"
            f"{o.fusegrid_allowed:>9}  ${o.fusegrid_spend:>9.2f}"
            f"   {'HELD' if o.held else 'BREACHED'}"
        )

    print()
    for o in outcomes:
        print(f"{o.scenario}: {o.note}")

    breached = [o for o in outcomes if not o.held]
    out = Path(__file__).parent / "results" / "replay.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "ceiling_usd": CEILING,
                "cost_per_call_usd": COST,
                "requests": REQUESTS,
                "scenarios": [
                    {
                        "scenario": o.scenario,
                        "baseline_allowed": o.baseline_allowed,
                        "baseline_spend_usd": o.baseline_spend,
                        "fusegrid_allowed": o.fusegrid_allowed,
                        "fusegrid_spend_usd": round(o.fusegrid_spend, 6),
                        "ceiling_held": o.held,
                        "note": o.note,
                    }
                    for o in outcomes
                ],
            },
            indent=2,
        )
        + "\n"
    )

    print()
    print(f"{len(outcomes) - len(breached)} of {len(outcomes)} scenarios: ceiling held.")
    print(f"raw results → {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")

    if breached:
        print("\nCEILING BREACHED — criterion 1 not met:")
        for o in breached:
            print(f"  {o.scenario}: spent ${o.fusegrid_spend:.4f} against ${CEILING:.2f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
