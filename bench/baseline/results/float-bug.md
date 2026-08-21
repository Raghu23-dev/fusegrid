# The float bug the adversarial suite caught

Found before any deployment, by a test asserting that exactly 20 of 200 concurrent
$0.05 reservations should be admitted against a $1.00 ceiling.

**It admitted 19.**

```python
>>> t = 0.0
>>> for _ in range(20): t += 0.05
>>> repr(t)
'1.0000000000000002'
>>> t > 1.0
True
```

Twenty $0.05 reservations sum to $1.0000000000000002. That exceeds a $1.00 ceiling by
2.2e-16, so the twentieth **legitimate** request was denied.

## Why this mattered more than it looks

A budget that rejects a request it should allow is as broken as one that admits a
request it should not. The failure mode is worse in one respect: an over-strict budget
produces support tickets that look like bugs elsewhere, and the natural response is to
raise the ceiling — which quietly defeats the control.

It also would have been nearly impossible to diagnose in production. The error appears
only when reservations happen to sum near the ceiling, so it would present as
intermittent, unreproducible 429s.

## Fix

Money is stored as **integer micro-dollars**, never float. `MICROS = 1_000_000`, with
half-up rounding at the boundary so the quantisation is unbiased rather than
systematically over- or under-reserving.

Redis follows the same discipline: `INCRBY` on integers rather than `INCRBYFLOAT`,
which carries the identical accumulation error.

One micro-dollar is far finer than any provider's per-token price and leaves headroom
for a ledger in the billions.

## Second-order consequence

The settlement-exactness property test then failed, because it asserted agreement to
float epsilon while the store deliberately quantises to integers. The tolerance was
corrected to one micro-dollar per settle — agreement finer than the unit of account is
meaningless. That is a test fix, not a code fix, and the distinction is recorded so a
reader can see which was which.
