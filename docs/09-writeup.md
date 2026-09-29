# fusegrid — Technical Writeup

> **Gate:** all six sections present before the project counts as shipped.
> Hand-written prose. This document is the primary evidence of technical communication.

## 1. Problem

LLM spend limits do not stop spending. You configure a budget, exceed it, and the
request goes through anyway — you find out on the invoice. This isn't a bug in one
product; it's a property of how enforcement is normally built. The cost of a call is
unknowable until the response arrives, so accounting happens after the money is spent,
against state that by construction excludes the request in flight.

I measured this rather than citing it. Four conventional enforcement patterns, against a
$1.00 ceiling at $0.05 per call, 40 requests where 20 should be admitted:

| Pattern | Allowed | Spend | Overrun |
|---|---|---|---|
| check-then-act race | 40 | $2.00 | 100% |
| post-hoc accounting | 20 | $1.95 | 95% |
| best-effort recording | 40 | $2.00 | 100% |
| unpriced model fallback | 40 | $2.00 | 100% |

**4 of 4 failed open**, each reproducible in under a second with no credentials and no
network. The post-hoc case is the one that should worry people most, because it doesn't
even need concurrency to break: nineteen cheap calls burn $0.95, and the twentieth — a
long completion costing twenty times more — is admitted because $0.05 of headroom still
shows on the ledger. And best-effort recording, which looks like resilience ("don't fail
the user's request over a telemetry write"), is the most dangerous of the four: when the
ledger write fails, the request proceeds anyway, and enforcement is unbounded for as long
as the store stays degraded.

The stakes are asymmetric with normal cost-control problems. An agent that calls a model
in a loop has no user in it to notice a runaway bill — a malformed tool response that
triggers a retry can issue thousands of calls in minutes, and published incidents
describe overruns discovered a full billing cycle later (one case: 860% over, $1.8M,
undetected for five months). 98% of FinOps teams now report managing AI spend, which
means the control is *expected* to exist — a budget that's configured but silently
doesn't enforce produces false confidence, which is worse than no budget at all.

## 2. Architecture

The one idea: **reserve before you spend, settle after you know.** Before the upstream
call, atomically reserve the *maximum possible* cost of the request. A request is
admitted only if that reservation fits inside the remaining balance. After the response
lands, settle the reservation to the actual cost and release the difference.

```
price → reserve → call upstream → settle
```

Every measured failure traces to enforcing *after* the money is already spent — reserve
before the call is the only place in the flow where a request can still be stopped
instead of merely counted. That's not a novel technique (it's standard in payments); the
contribution is applying it at the LLM transport layer, where nothing open-source does.

Each rejected alternative maps directly onto a specific failure from step 1:

- **Enforcement point — before the call, not after usage lands.** Cost can't be
  un-spent, so anything checking after the response is structurally the post-hoc
  accounting failure, just moved.
- **Reservation amount — the configured maximum, not an estimate from prompt tokens.**
  Estimating cost ahead of a call is a research problem with unbounded error. Reserving
  the max is exact and boring, and boring is the right property for money.
- **Unknown model — deny, not a default price.** A default price silently mis-enforces
  for exactly the newest, typically most expensive models, since pricing tables lag
  releases. This is the unpriced-model failure, reproduced by trying to be helpful.
- **Ledger failure — deny (fail closed), not proceed and record best-effort.** This is
  the design's sharp edge and the decision most likely to get argued with, which is why
  it's stated here rather than buried: a budget control that degrades to permissive when
  its store is unreachable isn't a control, it's a control-shaped object.
- **Store — Redis single-node, one atomic `INCRBYFLOAT` Lua script**, not a Postgres row
  lock or an in-process counter. A script is one round trip with real atomicity; an
  in-process counter breaks the moment there's more than one replica, which is the exact
  race behind the check-then-act failure.
- **Streaming — pass through untouched, settle on the final chunk**, not buffer to count
  tokens. Buffering destroys time-to-first-token, which is the entire reason to stream in
  the first place.
- **Deploy shape — sidecar, not a library.** A library has to be adopted per-service and
  can be bypassed by code that doesn't import it. A sidecar sits in front of every
  request regardless of what the calling code knows about it.

The invariant every adversarial test exists to violate:

```
committed + Σ(open reservations) ≤ ceiling
```

## 3. Decisions

The trade-offs worth arguing with are the availability ones, and I'd rather state them
than have them discovered. **Denying an unpriced model** means a model missing from the
pricing table gets refused, not admitted for free — the alternative (assume a default
price) is exactly how failure #4 happens, just relabeled as a feature. **Failing closed
on a ledger outage** means a Redis blip turns into 429s across the board rather than
unmetered spend — the alternative is failure #3 wearing a "graceful degradation" label.
Both trade a small amount of availability for a control that's actually a control; what
would change either is a caller *explicitly* opting into fail-open, documented as unsafe,
which is different from it happening by default because no one thought about the outage
case.

The other load-bearing decision is reserving the configured maximum rather than a
predicted cost. It's the least interesting-sounding choice in the system and the one I'd
defend hardest: prediction has unbounded error and a wrong prediction that under-reserves
is a silent re-run of the post-hoc failure with extra steps. Over-reserving is
recoverable at settlement; under-reserving is not recoverable at all, because the money's
already gone by the time you find out you guessed low.

## 4. Benchmarks

Three harnesses, each gating on its own threshold rather than just reporting a number:
`bench/baseline/failopen.py` records the problem (four conventional patterns, 40
requests, no network or credentials needed to reproduce); `bench/enforce/replay.py`
replays the identical four patterns against fusegrid and exits non-zero on any ceiling
breach; `bench/latency/measure.py` runs the enforcement path alone — deliberately
excluding the upstream call, since including it would bury microsecond-scale overhead
under hundreds of milliseconds of model latency — for 5 runs × 2,000 requests, reporting
p50/p95/p99/mean per run so a single lucky run can't pass as the result.

## 5. Results

All six pre-registered criteria met. The headline is the same table as the problem
statement, replayed through fusegrid instead of around it:

| Scenario | Baseline allowed / spend | fusegrid allowed / spend | Ceiling |
|---|---|---|---|
| check-then-act race | 40 / $2.00 | **20 / $1.00** | held |
| post-hoc accounting | 20 / $1.95 | **19 / $0.95** | held |
| best-effort recording | 40 / $2.00 | **0 / $0.00** | held |
| unpriced model | 40 / $2.00 | **0 / $0.00** | held |

**0 of 4 held in the baseline. 4 of 4 hold here.** Latency overhead is measured, not
assumed: worst p99 across five runs of 2,000 requests is **0.0035 ms** against a 15 ms
threshold — four orders of magnitude of headroom — with a p99 spread of only 0.0002 ms
across runs, so the number is stable rather than a fluke. That figure is explicitly
scoped to the enforcement path with an in-process store; a Redis store adds one network
round trip (typically 0.2–1 ms locally), which is real overhead this measurement does not
include. Settlement holds to micro-dollar resolution.

What came out worse than expected, published rather than smoothed over: a **float bug
denied a legitimate request** — twenty $0.05 reservations sum to $1.0000000000000002 in
IEEE754, so the twentieth *valid* request was refused against a $1.00 ceiling by an error
of 2.2e-16. In production that would have presented as intermittent, unreproducible 429s.
Money is now integer micro-dollars. Separately, an **empty usage block was costed at
zero**, which would have released a whole reservation for a request the provider
actually billed — the same failure mode as an unpriced model, wearing a different
disguise. And my own test helper *hid* that second bug: a pattern like `usage or
{default}` treats an empty dict as "not provided" and silently substitutes the default,
which is exactly the case under test. My first post-hoc baseline scenario was also wrong
in the other direction — it reported 0% overrun because it checked equality against the
ceiling, when a real proxy admits any request with headroom remaining regardless of how
much. Corrected to the 95%-over, no-concurrency-needed version above.

## 6. Limitations

The reservation is the configured maximum, not an estimate — a request that reserves
$0.50 and actually costs $0.01 holds the full $0.50 until it settles, so under heavy
concurrency with generous `max_tokens`, a budget can look exhausted while very little has
actually been spent. This is a deliberate trade against the alternative (predicting cost
and getting it wrong in the direction that matters).

Input token counting is a 4-characters-per-token approximation, biased high — no real
tokenizer per model family is implemented. Open reservations live in process memory, so a
crashed process leaks its reservations until `sweep_expired` reclaims them (15 minutes by
default); persisting them instead would add a round trip to the hot path this system
exists to keep fast. `MemoryStore` is single-replica only — behind a load balancer it
reintroduces the exact check-then-act race this whole design was built to close, which is
why `RedisStore` is required for more than one instance. There's no output-token
enforcement mid-stream: terminating a stream partway leaves the caller with a truncated
response that's still billed, and reserving the maximum upfront avoids that situation
rather than solving it.
