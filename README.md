# fusegrid

**A fuse for LLM spend.** Budgets that actually block, enforced before the upstream call.

You configure a spend limit, exceed it, and nothing stops the request — you find out on
the invoice. This is not a bug in one product; it is a property of enforcing a limit
against a cost that is unknowable until after the money is spent.

<!-- SCREENCAST: Act 1 — a runaway agent loop burning past its budget, counter climbing.
     Act 2 — same loop, blocked mid-flight at the ceiling.
     Act 3 — the enforcement table and the command that regenerates it. -->

## The measured problem

Four enforcement patterns, $1.00 ceiling, $0.05 per call, 40 requests. Twenty calls
should be permitted.

| Pattern | Allowed | Spend | Overrun | Failed open |
|---|---|---|---|---|
| check-then-act race | 40 | $2.00 | 100% | yes |
| post-hoc accounting | 20 | $1.95 | 95% | yes |
| best-effort recording | 40 | $2.00 | 100% | yes |
| unpriced model fallback | 40 | $2.00 | 100% | yes |

**4 of 4 failed open.** Reproduce in under a second, no credentials, no network:

```bash
python bench/baseline/failopen.py
```

## The result

The same four scenarios, replayed against fusegrid:

| Scenario | Baseline allowed | Baseline spend | fusegrid allowed | fusegrid spend | Ceiling |
|---|---|---|---|---|---|
| check-then-act race | 40 | $2.00 | **20** | **$1.00** | **held** |
| post-hoc accounting | 20 | $1.95 | **19** | **$0.95** | **held** |
| best-effort recording | 40 | $2.00 | **0** | **$0.00** | **held** |
| unpriced model | 40 | $2.00 | **0** | **$0.00** | **held** |

```bash
python bench/enforce/replay.py   # exits non-zero if any ceiling is breached
```

## How it works

```
price → reserve → call upstream → settle
```

Reserve the **maximum possible** cost before the call, atomically. Settle to the actual
cost after, releasing the difference. A request is admitted only if its reservation fits
inside the remaining balance.

The invariant, which every test in `tests/adversarial/` tries to violate:

```
committed + Σ(open reservations) ≤ ceiling
```

Each measured failure is inverted by one property of that design:

| Failure | Why it cannot happen here |
|---|---|
| check-then-act race | The reservation is one atomic operation |
| post-hoc accounting | Cost is committed *before* the call, not after |
| best-effort recording | A failed reservation **denies** rather than proceeding |
| unpriced model | No price means no maximum, so nothing can be reserved |

## It fails closed, deliberately

If the ledger is unreachable, requests are **denied**. A budget control that degrades to
permissive when its store is unavailable is not a control — it is a control-shaped
object, and that is measured failure #3.

An unpriced model is likewise denied rather than costed at zero. Pricing tables lag
model releases, so treating an unknown model as free removes enforcement precisely for
the newest and typically most expensive models.

Both are availability trade-offs. They are stated here rather than buried because they
are the decisions most likely to be argued with.

## Quickstart

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest                          # 45 adversarial tests
python bench/enforce/replay.py  # the headline result
```

## Verify the claims yourself

```bash
python bench/baseline/failopen.py   # four patterns fail open
python bench/enforce/replay.py      # fusegrid holds all four
pytest tests/adversarial -q         # 45 tests attacking the invariant
```

Every number in this README comes from one of those. `bench/` is committed and never
gitignored.

## The bug the test suite caught before any deployment

A test asserted that exactly 20 of 200 concurrent $0.05 reservations should be admitted
against a $1.00 ceiling. **It admitted 19.**

```python
>>> t = 0.0
>>> for _ in range(20): t += 0.05
>>> t > 1.0
True     # 1.0000000000000002
```

Twenty $0.05 reservations exceed $1.00 by 2.2e-16, so the twentieth *legitimate* request
was denied. A budget that rejects a request it should allow is as broken as one that
admits a request it should not — and in production it would have presented as
intermittent, unreproducible 429s.

Money is now stored as integer micro-dollars. Redis uses `INCRBY`, not `INCRBYFLOAT`,
which carries the same error. Full writeup: `bench/baseline/results/float-bug.md`.

## Limitations

- **The reservation is the configured maximum, not an estimate.** A request that reserves
  $0.50 and costs $0.01 holds $0.50 until it settles. Under heavy concurrency with
  generous `max_tokens`, a budget can appear exhausted while little has actually been
  spent. Estimation is a research problem with unbounded error; over-reserving is
  recoverable at settlement and under-reserving is not.
- **Input token counting is a 4-characters-per-token approximation**, biased high. A real
  tokeniser per model family would be more accurate and is not implemented.
- **Open reservations live in process memory.** A crashed process leaks its reservations
  until `sweep_expired` reclaims them (default 15 minutes). Persisting them would add a
  round-trip to the hot path.
- **`MemoryStore` is single-replica only.** Behind a load balancer it reintroduces
  measured failure #1 exactly. Use `RedisStore` for more than one instance.
- **Latency overhead is not yet measured.** Criterion 3 (p99 < 15 ms) is unverified until
  `bench/latency/` exists, and this README will not claim it before then.
- **No output-token enforcement mid-stream.** Terminating a stream partway leaves the
  caller with a truncated response and still billed. Reserving the maximum upfront avoids
  the situation rather than solving it.

## Licence

MIT — see [LICENSE](LICENSE).
