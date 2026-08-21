# 01 — The Problem

> **Gate:** contains numbers I measured myself. Harness: `bench/baseline/failopen.py`.
> Raw results: `bench/baseline/results/failopen.json`.

## Statement

**LLM spend limits do not stop spending.** You configure a budget, exceed it, and
nothing blocks the request. You find out on the invoice.

This is not a gap in one product. It is a property of how enforcement is normally
built: the cost of a call is unknowable until the response arrives, so accounting
happens after the money is spent, and the check that was supposed to prevent the spend
runs against state that excludes it.

## Why it matters

An agent that can call a model in a loop is unbounded financial exposure with no
circuit breaker. Three things make this worse than a normal cost-control problem:

1. **The blast radius is a retry loop, not a user.** A malformed tool response that
   triggers a retry can issue thousands of calls in minutes.
2. **Detection lags by a billing cycle.** Published incidents describe overruns
   discovered months after they began — an 860% / $1.8M case went five months
   undetected.
3. **98% of FinOps teams now report managing AI spend**, so the control is expected to
   exist. Configuring a budget that silently does not enforce is worse than having
   none, because it produces false confidence.

## The measured baseline

**Measured on:** 2026-08-21 · **Harness:** `python bench/baseline/failopen.py`
**Setup:** $1.00 ceiling, $0.05 per call, 40 requests. 20 calls should be permitted.

| Enforcement pattern | Allowed | Spend | Overrun | Failed open |
|---|---|---|---|---|
| check-then-act race | 40 | $2.00 | **100%** | yes |
| post-hoc accounting | 20 | $1.95 | **95%** | yes |
| best-effort recording | 40 | $2.00 | **100%** | yes |
| unpriced model fallback | 40 | $2.00 | **100%** | yes |

**4 of 4 patterns failed open.** Each is reproducible in under a second with no
credentials and no network, so the claim is checkable rather than trusted.

### What each failure actually is

**1. check-then-act race.** Read the counter, decide, then spend. Forty concurrent
requests all read the same under-ceiling value before any had written its cost back, so
all forty were permitted. This is the one that matters most in practice, because agent
frameworks fan out by design.

**2. post-hoc accounting.** The cost of a call is only known from its response, so the
ceiling is checked against state that excludes the in-flight request. Nineteen cheap
calls take spend to $0.95; the twentieth is a long completion costing twenty times more
and is admitted because $0.05 of headroom remains. **Sequential, not even concurrent** —
this one does not need a race to break.

**3. best-effort recording.** When the ledger write fails, the request proceeds anyway
so as not to "fail the user's request over telemetry." The ledger recorded $0.00 while
the provider billed $2.00. Enforcement is unbounded for as long as the store is
degraded. This is the most dangerous of the four because it looks like resilience.

**4. unpriced model fallback.** A model absent from the pricing table costs $0.00, so it
passes every ceiling check. Pricing tables lag model releases, which means the failure
targets the newest and typically most expensive models specifically.

## Prior art

Citations support the measurement and do not replace it. The dominant open-source LLM
proxy has documented budget failures including a report of an over-budget request
returning HTTP 200 rather than blocking. Nothing in the open-source ecosystem enforces
at the transport layer — the layer where a block is still possible.

## Reproduce

```bash
python bench/baseline/failopen.py
```
