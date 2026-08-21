"""ADVERSARIAL: try to breach the ceiling.

The invariant is `committed + Σ(open reservations) ≤ ceiling`. Every test here attacks
it using a pattern that `bench/baseline/failopen.py` showed defeating conventional
enforcement. Each test names the failure it prevents.

The measured baseline: four patterns, all failed open, 95–100% overrun. If those same
patterns cannot get through here, the thesis holds.
"""

from __future__ import annotations

import random
import threading

import pytest

from fusegrid import Ledger, LedgerUnavailable, MemoryStore, ModelPrice, Pricing, UnpricedModel

CEILING = 1.00
KEY = "team-a"


def build(ceiling: float = CEILING) -> tuple[Ledger, MemoryStore]:
    store = MemoryStore()
    return Ledger(store, {KEY: ceiling}), store


class TestCheckThenActRace:
    """Prevents baseline failure #1: 40 concurrent requests all admitted, 100% over."""

    def test_concurrent_reservations_cannot_exceed_ceiling(self) -> None:
        ledger, store = build()
        cost = 0.05
        expected_allowed = int(CEILING / cost)

        allowed: list[bool] = []
        lock = threading.Lock()

        def attempt() -> None:
            d = ledger.reserve(KEY, cost)
            with lock:
                allowed.append(d.allowed)

        threads = [threading.Thread(target=attempt) for _ in range(200)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(allowed) == expected_allowed, (
            f"admitted {sum(allowed)} of 200 concurrent requests, expected exactly "
            f"{expected_allowed}"
        )
        assert store.spent(KEY) <= CEILING + 1e-9

    @pytest.mark.parametrize("concurrency", [1, 10, 50, 200])
    def test_no_breach_at_any_concurrency(self, concurrency: int) -> None:
        """Criterion 1: zero breaches at 1, 10, 50, 200 concurrent."""
        ledger, store = build()
        cost = 0.05

        def attempt() -> None:
            ledger.reserve(KEY, cost)

        threads = [threading.Thread(target=attempt) for _ in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert store.spent(KEY) <= CEILING + 1e-9, (
            f"ceiling breached at concurrency {concurrency}: spent {store.spent(KEY)}"
        )


class TestPostHocAccounting:
    """Prevents baseline failure #2: a cheap-then-expensive sequence, 95% over."""

    def test_expensive_call_cannot_slip_through_remaining_headroom(self) -> None:
        ledger, store = build()

        # 19 cheap reservations take spend to 0.95.
        for _ in range(19):
            assert ledger.reserve(KEY, 0.05).allowed

        # A call whose maximum is 20x cheap. Conventional enforcement admits this
        # because 0.05 of headroom remains and it does not know the cost yet.
        # fusegrid reserves the MAXIMUM, so it does not fit and is denied.
        d = ledger.reserve(KEY, 1.00)
        assert not d.allowed
        assert d.reason == "budget_exceeded"
        assert store.spent(KEY) <= CEILING + 1e-9

    def test_settlement_releases_unused_reservation(self) -> None:
        """Over-reserving must not permanently waste budget, or nobody would deploy it."""
        ledger, store = build()

        d = ledger.reserve(KEY, 0.90)
        assert d.allowed and d.reservation is not None
        assert store.spent(KEY) == pytest.approx(0.90)

        # The call actually cost 0.10.
        released = ledger.settle(d.reservation.id, actual=0.10)
        assert released == pytest.approx(0.80)
        assert store.spent(KEY) == pytest.approx(0.10)

        # The released headroom is genuinely usable again.
        assert ledger.reserve(KEY, 0.85).allowed


class TestFailClosed:
    """Prevents baseline failure #3: ledger down, 40 calls admitted, ledger recorded 0."""

    def test_store_failure_denies_rather_than_proceeding(self) -> None:
        ledger, store = build()
        store.fail = True

        with pytest.raises(LedgerUnavailable):
            ledger.reserve(KEY, 0.01)

    def test_no_spend_recorded_while_store_is_down(self) -> None:
        ledger, store = build()
        store.fail = True
        for _ in range(40):
            with pytest.raises(LedgerUnavailable):
                ledger.reserve(KEY, 0.05)
        store.fail = False
        # The full ceiling is still available: nothing was admitted, so nothing was spent.
        assert store.spent(KEY) == pytest.approx(0.0)
        assert ledger.reserve(KEY, 1.00).allowed


class TestUnpricedModel:
    """Prevents baseline failure #4: unknown model costed at zero, 100% over."""

    def test_unknown_model_raises_rather_than_costing_zero(self) -> None:
        pricing = Pricing({"known-model": ModelPrice(1.0, 2.0)})
        with pytest.raises(UnpricedModel):
            pricing.max_cost("brand-new-model", input_tokens=1000, max_output_tokens=1000)

    def test_error_names_the_model_and_the_policy(self) -> None:
        pricing = Pricing({"known-model": ModelPrice(1.0, 2.0)})
        with pytest.raises(UnpricedModel, match="brand-new-model"):
            pricing.price("brand-new-model")
        try:
            pricing.price("brand-new-model")
        except UnpricedModel as exc:
            # The message must tell an operator what to do, not just what failed.
            assert "denies unpriced models" in str(exc)
            assert "pricing config" in str(exc)

    def test_known_model_prices_the_maximum_not_an_estimate(self) -> None:
        pricing = Pricing({"m": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0)})
        # 1000 in, up to 2000 out → 0.003 + 0.030
        assert pricing.max_cost("m", 1000, 2000) == pytest.approx(0.033)
        # Actual usage came in lower.
        assert pricing.actual_cost("m", 1000, 100) == pytest.approx(0.0045)


class TestSettlementExactness:
    """Criterion 6: reserved − released = actual, to the token."""

    def test_settlement_is_exact_over_random_sequences(self) -> None:
        """Exact to micro-dollar resolution, which is the ledger's unit.

        Tolerance is one micro-dollar per settle rather than float epsilon: the store
        deliberately quantises to integers, so agreement to a finer resolution than the
        unit of account would be meaningless. 20 settles → at most 20 micro-dollars of
        rounding, i.e. 0.002 cents.
        """
        rng = random.Random(1234)
        settles = 20
        for _ in range(200):
            ledger, store = build(ceiling=100.0)
            total_actual = 0.0
            for _ in range(settles):
                reserve_amount = rng.uniform(0.01, 1.0)
                d = ledger.reserve(KEY, reserve_amount)
                assert d.allowed and d.reservation is not None
                actual = rng.uniform(0.0, reserve_amount)
                ledger.settle(d.reservation.id, actual)
                total_actual += actual
            assert store.spent(KEY) == pytest.approx(total_actual, abs=settles / 1_000_000)

    def test_duplicate_settle_is_rejected_not_double_counted(self) -> None:
        """A retried webhook or a stream reporting usage twice must not corrupt state."""
        ledger, _ = build()
        d = ledger.reserve(KEY, 0.50)
        assert d.reservation is not None
        ledger.settle(d.reservation.id, 0.20)
        with pytest.raises(KeyError):
            ledger.settle(d.reservation.id, 0.20)

    def test_actual_above_reservation_is_clamped_never_under_reported(self) -> None:
        """The provider already billed it. Clamping keeps the ledger from under-counting."""
        ledger, store = build()
        d = ledger.reserve(KEY, 0.10)
        assert d.reservation is not None
        released = ledger.settle(d.reservation.id, actual=0.50)
        assert released == pytest.approx(0.0)
        assert store.spent(KEY) == pytest.approx(0.10)

    def test_negative_actual_is_treated_as_zero(self) -> None:
        ledger, store = build()
        d = ledger.reserve(KEY, 0.10)
        assert d.reservation is not None
        ledger.settle(d.reservation.id, actual=-5.0)
        assert store.spent(KEY) == pytest.approx(0.0)


class TestAbandonedReservations:
    """A client that vanishes must not hold budget forever."""

    def test_release_returns_the_full_amount(self) -> None:
        ledger, store = build()
        d = ledger.reserve(KEY, 0.60)
        assert d.reservation is not None
        ledger.release(d.reservation.id)
        assert store.spent(KEY) == pytest.approx(0.0)

    def test_double_release_is_harmless(self) -> None:
        ledger, store = build()
        d = ledger.reserve(KEY, 0.60)
        assert d.reservation is not None
        ledger.release(d.reservation.id)
        ledger.release(d.reservation.id)
        assert store.spent(KEY) == pytest.approx(0.0)

    def test_sweep_releases_stale_reservations(self) -> None:
        ledger, store = build()
        d = ledger.reserve(KEY, 0.80)
        assert d.reservation is not None
        assert ledger.open_reservations == 1
        swept = ledger.sweep_expired(max_age_seconds=0.0)
        assert swept == 1
        assert ledger.open_reservations == 0
        assert store.spent(KEY) == pytest.approx(0.0)

    def test_sweep_leaves_fresh_reservations_alone(self) -> None:
        ledger, _ = build()
        d = ledger.reserve(KEY, 0.10)
        assert d.reservation is not None
        assert ledger.sweep_expired(max_age_seconds=900.0) == 0
        assert ledger.open_reservations == 1


class TestHostileInput:
    def test_negative_reservation_is_rejected(self) -> None:
        """A negative reservation would increase available balance."""
        ledger, store = build()
        d = ledger.reserve(KEY, -1.0)
        assert not d.allowed
        assert d.reason == "invalid_amount"
        assert store.spent(KEY) == pytest.approx(0.0)

    def test_unknown_budget_key_is_denied(self) -> None:
        ledger, _ = build()
        d = ledger.reserve("no-such-team", 0.01)
        assert not d.allowed
        assert d.reason == "unknown_budget_key"

    def test_zero_reservation_is_allowed_and_harmless(self) -> None:
        ledger, store = build()
        assert ledger.reserve(KEY, 0.0).allowed
        assert store.spent(KEY) == pytest.approx(0.0)

    def test_reservation_exactly_at_ceiling_is_allowed(self) -> None:
        """Off-by-one at the boundary: exactly the ceiling must fit."""
        ledger, _ = build()
        assert ledger.reserve(KEY, CEILING).allowed

    def test_one_cent_over_ceiling_is_denied(self) -> None:
        ledger, _ = build()
        assert not ledger.reserve(KEY, CEILING + 0.01).allowed

    def test_denial_reports_remaining_balance(self) -> None:
        """Criterion 2: a denial must be legible enough to act on."""
        ledger, _ = build()
        assert ledger.reserve(KEY, 0.70).allowed
        d = ledger.reserve(KEY, 0.50)
        assert not d.allowed
        assert d.remaining == pytest.approx(0.30)
        assert d.ceiling == pytest.approx(CEILING)
        assert d.retry_after_seconds is None  # not fabricated
