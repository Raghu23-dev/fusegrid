# 02 — Thesis and Success Criteria

> **Gate:** committed **before the first feature commit**. Git history is the proof
> these criteria were not fitted to the results.

## Thesis

**A token budget can be enforced before the upstream call, such that no request
pattern — concurrent, variable-cost, storage-degraded, or unpriced — exceeds a hard
ceiling, at a latency cost small enough to be irrelevant next to model latency.**

Falsifiable two ways: either a pattern gets through the ceiling, or enforcement costs
enough latency that no one would deploy it.

## The mechanism this rests on

Reserve-then-settle. Before the upstream call, atomically reserve the *maximum possible*
cost of the request. After the response, settle to the actual cost and release the
difference. A request is admitted only if the reservation fits inside the remaining
balance.

This directly inverts each measured failure:

| Measured failure | Why reserve-settle prevents it |
|---|---|
| check-then-act race | The reservation is atomic, so concurrent requests cannot all see the same balance |
| post-hoc accounting | Cost is committed *before* the call, not after |
| best-effort recording | A failed reservation **denies** the request rather than proceeding |
| unpriced model | An unknown model has no maximum, so it cannot be reserved and is denied |

The last row is the design's sharp edge: **an unpriceable request is refused, not
waved through.** That is a deliberate availability trade-off, and stating it upfront
matters because it is the decision most likely to be argued with.

## Success criteria

| # | Criterion | Threshold | How measured |
|---|---|---|---|
| 1 | **No ceiling breach under concurrency** | **0** breaches at 1, 10, 50, 200 concurrent | `bench/enforce/` replays all four baseline patterns against fusegrid |
| 2 | **Denial is correct and legible** | HTTP 429, machine-readable reason, remaining balance | Adversarial suite asserts response shape |
| 3 | **Latency overhead** | **p99 < 15 ms** added vs passthrough | `bench/latency/`, ≥5 runs, variance reported |
| 4 | **Unpriced models denied, never zero-costed** | 100% denied with an explicit reason | Adversarial suite |
| 5 | **Storage failure fails closed** | 100% denied while the store is down | Fault-injection suite |
| 6 | **Settlement is exact** | reserved − released = actual, to the token | Property test over randomised cost sequences |

## Kill conditions

- **If reserve-settle cannot hold under concurrency without serialising requests** to
  the point of unusable latency, publish the tradeoff curve rather than the mechanism.
  "Here is what enforcement costs" is a useful result.
- **If p99 overhead exceeds 15 ms**, report it plainly. A correct sidecar nobody deploys
  because it is slow has failed.
- **If denying unpriced models proves unworkable in practice**, document why and ship a
  configurable policy — but the default stays deny, and the reasoning gets published.

## Explicitly not claimed

- Not claiming the incumbent is badly engineered. Post-hoc accounting is the *natural*
  design when cost is unknowable in advance; the measurement shows the consequence, not
  incompetence.
- Not claiming novelty for reserve-settle. It is standard in payments. The contribution
  is applying it at the LLM transport layer, where nothing open-source does.

## Out of scope

See `NON-GOALS.md`.
