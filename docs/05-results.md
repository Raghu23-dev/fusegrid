# 05 — Benchmarks and Results

> No number is claimed unless it comes from `bench/` and is reproducible with one
> command. `bench/` is committed and never gitignored.

## Harnesses

| Harness | Measures | Gate |
|---|---|---|
| `bench/baseline/failopen.py` | Four conventional patterns failing open | records the problem |
| `bench/enforce/replay.py` | The same four against fusegrid | exit 1 on any breach |
| `bench/latency/measure.py` | Enforcement overhead percentiles | exit 1 over 15 ms p99 |

## Against the pre-registered criteria

| # | Criterion | Threshold | Result | Pass |
|---|---|---|---|---|
| 1 | No ceiling breach under concurrency | 0 at 1/10/50/200 | **0 breaches**, all four scenarios held | yes |
| 2 | Denial is legible | code + reason + remaining | **stable codes**, machine-readable body | yes |
| 3 | Latency overhead | p99 < 15 ms | **0.0035 ms** worst p99 | yes |
| 4 | Unpriced models denied | 100% | **40/40 denied**, never zero-costed | yes |
| 5 | Storage failure fails closed | 100% denied | **40/40 denied**, $0.00 spent | yes |
| 6 | Settlement exact | to the token | exact to **micro-dollar** resolution | yes |

## The headline: baseline vs enforced

$1.00 ceiling, $0.05 per call, 40 requests. Twenty calls should be permitted.

| Scenario | Baseline allowed | Baseline spend | fusegrid allowed | fusegrid spend | Ceiling |
|---|---|---|---|---|---|
| check-then-act race | 40 | $2.00 (100% over) | **20** | **$1.00** | **held** |
| post-hoc accounting | 20 | $1.95 (95% over) | **19** | **$0.95** | **held** |
| best-effort recording | 40 | $2.00 (100% over) | **0** | **$0.00** | **held** |
| unpriced model | 40 | $2.00 (100% over) | **0** | **$0.00** | **held** |

**0 of 4 held in the baseline. 4 of 4 hold here.**

## Latency

5 runs × 2,000 requests, enforcement path only:

| Run | p50 | p95 | p99 | mean |
|---|---|---|---|---|
| 1 | 0.0028 | 0.0030 | 0.0035 | 0.0029 |
| 2 | 0.0027 | 0.0029 | 0.0034 | 0.0028 |
| 3 | 0.0027 | 0.0030 | 0.0035 | 0.0027 |
| 4 | 0.0027 | 0.0030 | 0.0035 | 0.0028 |
| 5 | 0.0026 | 0.0028 | 0.0033 | 0.0026 |

All in milliseconds. **p99 spread across runs: 0.0002 ms** — the measurement is stable,
not a lucky run.

Worst p99 **0.0035 ms** against a 15 ms threshold, four orders of magnitude of headroom.

**Scope stated honestly:** this measures the enforcement path with an in-process store,
and deliberately excludes the upstream call — including it would bury the overhead under
hundreds of milliseconds of model latency and make the number meaningless. A Redis store
adds one network round trip, typically 0.2–1 ms locally, which is **not measured here**.
The criterion is met with enormous margin either way, but the claim is scoped to what was
actually measured.

## What came out worse than expected

**A float bug denied a legitimate request.** Twenty $0.05 reservations sum to
$1.0000000000000002, exceeding a $1.00 ceiling by 2.2e-16, so the twentieth request was
refused. A budget that rejects a request it should allow is as broken as one that admits
a request it should not, and in production it would have presented as intermittent,
unreproducible 429s. Money is now integer micro-dollars.
Full writeup: `bench/baseline/results/float-bug.md`.

**An empty usage block was costed at zero.** A provider that serves a request but reports
no usage would have had its whole reservation released — measured failure #4 in a
different disguise. An absent token count now settles at the reservation.

**My own test helper hid that bug.** `usage or {default}` treats an empty dict as
"not provided", so the helper silently replaced the exact case under test. A test that
cannot express its own case is worse than no test.

**My first post-hoc baseline scenario was too weak**, showing 0% overrun. That was my
scenario being wrong, not the pattern being safe: I checked equality against the ceiling
while a real proxy admits any request with headroom remaining. Corrected to 95% over, and
sequential — that pattern does not even need a race to break.
