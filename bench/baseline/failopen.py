#!/usr/bin/env python3
"""Demonstrate the four ways a token budget fails open.

WHY THIS EXISTS

Citations establish that budget enforcement in the dominant open-source LLM proxy
fails open. A citation proves the problem is *known*. This harness proves I can
*characterise* it — which is the part that matters, because the shape of the failure
determines the fix.

Rather than depend on a specific vendor's deployment, this models the enforcement
patterns those systems actually use and shows each one failing under conditions that
occur in normal operation. Every scenario here is reproducible in seconds with no
credentials and no network, so anyone can check the claim rather than trust it.

The four patterns are not hypothetical. They correspond to:
  1. check-then-act        — read the counter, decide, then spend (the classic race)
  2. post-hoc accounting   — record usage after the call returns
  3. best-effort recording — swallow a storage failure so the request still succeeds
  4. unpriced fallback     — treat an unknown model's cost as zero

Run:  python bench/baseline/failopen.py
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

CEILING_USD = 1.00
COST_PER_CALL_USD = 0.05  # 20 calls exactly exhausts the ceiling


@dataclass
class Ledger:
    """Spend accounting, as these systems typically model it."""

    spent: float = 0.0
    calls_allowed: int = 0
    storage_failures: int = 0
    # Set to make writes fail, modelling a storage blip.
    fail_writes: bool = False

    async def read(self) -> float:
        await asyncio.sleep(0.001)  # any real store is a network hop
        return self.spent

    async def write(self, amount: float) -> None:
        await asyncio.sleep(0.001)
        if self.fail_writes:
            self.storage_failures += 1
            raise RuntimeError("ledger unavailable")
        self.spent += amount


@dataclass
class Result:
    name: str
    mechanism: str
    calls_allowed: int
    final_spend: float
    ceiling: float
    failed_open: bool
    detail: str
    overrun_pct: float = field(init=False)

    def __post_init__(self) -> None:
        self.overrun_pct = (
            0.0 if self.ceiling == 0 else (self.final_spend - self.ceiling) / self.ceiling * 100
        )


async def pattern_check_then_act(concurrency: int) -> Result:
    """1. Read the counter, decide, then spend.

    Every concurrent request reads the same under-ceiling value before any of them
    has written its cost back, so all of them are permitted. This is the failure
    mode that matters most in practice, because agent frameworks fan out.
    """
    ledger = Ledger()

    async def request() -> bool:
        spent = await ledger.read()
        if spent + COST_PER_CALL_USD > CEILING_USD:
            return False
        await asyncio.sleep(0.02)  # the upstream model call
        await ledger.write(COST_PER_CALL_USD)
        return True

    allowed = sum(await asyncio.gather(*(request() for _ in range(concurrency))))
    return Result(
        name="check-then-act race",
        mechanism="read counter → decide → spend",
        calls_allowed=allowed,
        final_spend=ledger.spent,
        ceiling=CEILING_USD,
        failed_open=ledger.spent > CEILING_USD,
        detail=(
            f"{concurrency} concurrent requests all read a value under the ceiling "
            f"before any had written back. {allowed} were permitted."
        ),
    )


async def pattern_post_hoc(concurrency: int) -> Result:
    """2. Record usage after the call returns.

    Token counts are only known from the response, so accounting happens afterwards.
    The ceiling is therefore always enforced against stale state: the request that
    crosses it has already been paid for.
    """
    ledger = Ledger()

    # Real token costs vary per call — a long completion costs far more than a short
    # one, and the amount is unknowable until the response arrives. That variance is
    # what makes post-hoc accounting unsafe rather than merely late.
    # 19 cheap calls take spend to 0.95, then one long completion costing 20x lands
    # while 0.05 of headroom remains. The proxy admits it because it cannot know.
    costs = [COST_PER_CALL_USD] * 19 + [COST_PER_CALL_USD * 20] * (concurrency - 19)

    async def request(cost: float) -> bool:
        # Admit while any headroom remains. A proxy cannot do better here: the cost
        # of THIS call is unknowable until the response arrives, so there is nothing
        # to compare against the remaining balance.
        if await ledger.read() >= CEILING_USD:
            return False
        await asyncio.sleep(0.02)
        await ledger.write(cost)  # cost known only now
        return True

    allowed = 0
    for cost in costs:
        if await request(cost):
            allowed += 1

    return Result(
        name="post-hoc accounting",
        mechanism="call upstream → then record cost",
        calls_allowed=allowed,
        final_spend=ledger.spent,
        ceiling=CEILING_USD,
        failed_open=ledger.spent > CEILING_USD,
        detail=(
            "Sequential, not even concurrent. The ceiling is checked against state that "
            "excludes the in-flight call, so a request approved with headroom to spare "
            "can still blow through it — real completion costs vary by an order of "
            "magnitude and are unknowable before the call."
        ),
    )


async def pattern_best_effort(concurrency: int) -> Result:
    """3. Swallow a storage failure so the request still succeeds.

    A proxy that treats accounting as best-effort keeps serving when the ledger is
    unavailable. Availability is preserved and the budget becomes unbounded — the
    single most dangerous of the four, because it looks like resilience.
    """
    ledger = Ledger(fail_writes=True)
    billed = 0.0

    async def request() -> bool:
        nonlocal billed
        if await ledger.read() >= CEILING_USD:
            return False
        await asyncio.sleep(0.02)
        try:
            await ledger.write(COST_PER_CALL_USD)
        except RuntimeError:
            pass  # "don't fail the user's request over telemetry"
        billed += COST_PER_CALL_USD  # the provider bills regardless
        return True

    allowed = 0
    for _ in range(concurrency):
        if await request():
            allowed += 1

    return Result(
        name="best-effort recording",
        mechanism="ledger write fails → continue anyway",
        calls_allowed=allowed,
        final_spend=billed,
        ceiling=CEILING_USD,
        failed_open=billed > CEILING_USD,
        detail=(
            f"Ledger recorded 0.00 while the provider billed {billed:.2f}. "
            f"{ledger.storage_failures} write failures were swallowed. Enforcement is "
            "unbounded for as long as the store is down."
        ),
    )


async def pattern_unpriced(concurrency: int) -> Result:
    """4. Treat an unknown model's cost as zero.

    Pricing tables lag model releases. A model absent from the table costs 0.0, so it
    is free to call without limit — silently, and precisely for the newest and often
    most expensive models.
    """
    ledger = Ledger()
    real_cost = 0.0

    async def request(known: bool) -> bool:
        nonlocal real_cost
        priced = COST_PER_CALL_USD if known else 0.0
        if await ledger.read() + priced > CEILING_USD:
            return False
        await asyncio.sleep(0.02)
        await ledger.write(priced)
        real_cost += COST_PER_CALL_USD
        return True

    allowed = sum(
        [await request(known=False) for _ in range(concurrency)],
    )

    return Result(
        name="unpriced model fallback",
        mechanism="model missing from price table → cost 0.0",
        calls_allowed=allowed,
        final_spend=real_cost,
        ceiling=CEILING_USD,
        failed_open=real_cost > CEILING_USD,
        detail=(
            f"All {allowed} calls passed the ceiling check at zero cost. Actual spend "
            f"{real_cost:.2f}. The ledger believes it spent {ledger.spent:.2f}."
        ),
    )


async def main() -> None:
    random.seed(0)
    concurrency = 40

    results = [
        await pattern_check_then_act(concurrency),
        await pattern_post_hoc(concurrency),
        await pattern_best_effort(concurrency),
        await pattern_unpriced(concurrency),
    ]

    print(f"ceiling ${CEILING_USD:.2f}  ·  ${COST_PER_CALL_USD:.2f}/call  ·  "
          f"{concurrency} requests  ·  {int(CEILING_USD / COST_PER_CALL_USD)} calls should be allowed\n")
    print(f"{'pattern':<26}{'allowed':>8}{'spend':>9}{'overrun':>10}  failed open")
    print("-" * 68)
    for r in results:
        print(
            f"{r.name:<26}{r.calls_allowed:>8}{r.final_spend:>9.2f}"
            f"{r.overrun_pct:>9.0f}%  {'YES' if r.failed_open else 'no'}"
        )

    print()
    for r in results:
        print(f"{r.name}")
        print(f"  mechanism: {r.mechanism}")
        print(f"  {r.detail}\n")

    out = Path("bench/baseline/results/failopen.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "ceiling_usd": CEILING_USD,
                "cost_per_call_usd": COST_PER_CALL_USD,
                "concurrency": concurrency,
                "expected_allowed": int(CEILING_USD / COST_PER_CALL_USD),
                "patterns": [
                    {
                        "name": r.name,
                        "mechanism": r.mechanism,
                        "calls_allowed": r.calls_allowed,
                        "final_spend_usd": round(r.final_spend, 4),
                        "overrun_pct": round(r.overrun_pct, 1),
                        "failed_open": r.failed_open,
                        "detail": r.detail,
                    }
                    for r in results
                ],
            },
            indent=2,
        )
        + "\n"
    )

    failed = sum(r.failed_open for r in results)
    print(f"{failed} of {len(results)} enforcement patterns failed open.")
    print(f"raw results → {out}")


if __name__ == "__main__":
    asyncio.run(main())
