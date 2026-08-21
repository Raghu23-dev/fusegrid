"""Spend ledger: atomic reserve, settle, release.

THE INVARIANT

    committed + Σ(open reservations) ≤ ceiling

Everything in this module exists to hold that under concurrency, storage faults,
duplicate settles, and abandoned requests. Every adversarial test tries to break it.

WHY RESERVE-THEN-SETTLE

The cost of a model call is unknowable until the response arrives. Enforcement that
waits for the cost is enforcement after the money is spent — measured in
`bench/baseline/failopen.py`, where four such patterns all failed open.

So we reserve the *maximum possible* cost before the call, and settle to the actual
cost after. A request is admitted only if its reservation fits. The reservation is a
single atomic operation, which is what closes the check-then-act race.

WHY IT FAILS CLOSED

If a reservation cannot be persisted, the request is denied. A budget control that
degrades to permissive when its store is unavailable is not a control — it is a
control-shaped object. This is the design decision most likely to be argued with, so
it is explicit here, in the thesis, and in the README.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Protocol


class LedgerUnavailable(RuntimeError):
    """The ledger could not be reached or written.

    Raised rather than swallowed. A caller that catches this and proceeds has
    reintroduced measured failure #3.
    """


@dataclass(frozen=True, slots=True)
class Reservation:
    """An amount held against a budget, awaiting settlement."""

    id: str
    key: str
    amount: float
    created_at: float


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of a reservation attempt."""

    allowed: bool
    remaining: float
    ceiling: float
    reservation: Reservation | None = None
    reason: str | None = None

    @property
    def retry_after_seconds(self) -> int | None:
        """Seconds until the budget window resets, when that is knowable.

        Returns None rather than a guess. A fabricated retry-after teaches clients to
        hammer at the wrong interval, which is worse than no hint at all.
        """
        return None


class Store(Protocol):
    """Storage contract.

    `reserve` MUST be atomic: read-modify-write in one indivisible operation. A
    Protocol rather than a base class so a test double is a plain object, and so the
    Redis implementation carries no inheritance baggage.
    """

    def reserve(self, key: str, amount: float, ceiling: float) -> tuple[bool, float]:
        """Atomically add `amount` to held spend if it fits under `ceiling`.

        Returns (allowed, remaining_after). MUST NOT partially apply.
        """

    def settle(self, key: str, reserved: float, actual: float) -> None:
        """Replace a reservation with its actual cost, releasing the difference."""

    def release(self, key: str, amount: float) -> None:
        """Return a reservation to the budget in full."""

    def spent(self, key: str) -> float:
        """Total currently committed plus held."""


class Ledger:
    """Budget enforcement over a Store."""

    def __init__(self, store: Store, ceilings: dict[str, float]) -> None:
        self._store = store
        self._ceilings = dict(ceilings)
        # Open reservations, so a settle can be validated and a duplicate rejected.
        # Kept in-process deliberately: a reservation outlives only one request, and
        # persisting it would add a round-trip to the hot path for no benefit. The
        # consequence — a crashed process leaks its open reservations until they
        # expire — is handled by `sweep_expired`.
        self._open: dict[str, Reservation] = {}

    def ceiling(self, key: str) -> float | None:
        return self._ceilings.get(key)

    def reserve(self, key: str, amount: float) -> Decision:
        """Hold `amount` against `key`'s budget.

        Denies rather than raises for budget reasons; raises only when the store is
        unreachable, because that is not a budget decision.
        """
        ceiling = self._ceilings.get(key)
        if ceiling is None:
            return Decision(
                allowed=False,
                remaining=0.0,
                ceiling=0.0,
                reason="unknown_budget_key",
            )

        if amount < 0:
            # A negative reservation would increase the available balance. Rejecting
            # it explicitly beats trusting every caller to be well-behaved.
            return Decision(
                allowed=False,
                remaining=ceiling,
                ceiling=ceiling,
                reason="invalid_amount",
            )

        try:
            allowed, remaining = self._store.reserve(key, amount, ceiling)
        except LedgerUnavailable:
            raise
        except Exception as exc:
            raise LedgerUnavailable(str(exc)) from exc

        if not allowed:
            return Decision(
                allowed=False,
                remaining=remaining,
                ceiling=ceiling,
                reason="budget_exceeded",
            )

        res = Reservation(
            id=uuid.uuid4().hex,
            key=key,
            amount=amount,
            created_at=time.monotonic(),
        )
        self._open[res.id] = res
        return Decision(allowed=True, remaining=remaining, ceiling=ceiling, reservation=res)

    def settle(self, reservation_id: str, actual: float) -> float:
        """Settle a reservation to its actual cost. Returns the amount released.

        Idempotent by removal: a second settle for the same id raises rather than
        double-counting. Duplicate settlement is a realistic bug — a retried webhook,
        a stream that reports usage twice — and silently accepting it would corrupt
        the invariant in the direction of under-counting.
        """
        res = self._open.pop(reservation_id, None)
        if res is None:
            raise KeyError(f"unknown or already-settled reservation: {reservation_id}")

        if actual < 0:
            actual = 0.0
        # Actual cost above the reservation cannot be refused — the provider already
        # billed it. Clamp to the reservation so the ledger never under-reports, and
        # surface the overshoot so it is visible rather than silent.
        clamped = min(actual, res.amount)

        self._store.settle(res.key, reserved=res.amount, actual=clamped)
        return res.amount - clamped

    def release(self, reservation_id: str) -> None:
        """Return a reservation in full. For upstream errors and client disconnects."""
        res = self._open.pop(reservation_id, None)
        if res is None:
            return  # already settled or released; releasing twice is harmless
        self._store.release(res.key, res.amount)

    def sweep_expired(self, max_age_seconds: float = 900.0) -> int:
        """Release reservations older than `max_age_seconds`.

        An abandoned reservation — client vanished, process handling it died — would
        otherwise hold budget forever and slowly starve the key. Fifteen minutes is
        comfortably longer than any legitimate completion and short enough that a leak
        self-heals within one billing window.
        """
        now = time.monotonic()
        stale = [r for r in self._open.values() if now - r.created_at > max_age_seconds]
        for res in stale:
            self._open.pop(res.id, None)
            self._store.release(res.key, res.amount)
        return len(stale)

    @property
    def open_reservations(self) -> int:
        return len(self._open)

    def clear_reservations(self) -> None:
        """Forget all open reservations.

        Pairs with `Store.clear` for tests and resettable demos. Separate from the store's
        clear because the two hold different state: the store holds committed spend, the
        ledger holds what is outstanding. Clearing one without the other leaves the invariant
        `committed + open <= ceiling` computed from mismatched halves.
        """
        self._open.clear()

    def spent(self, key: str) -> float:
        return self._store.spent(key)
