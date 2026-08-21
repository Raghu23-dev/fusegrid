"""Tests for the deployed instance.

The property is not "the endpoints return 200". It is that the ceiling holds across an HTTP
boundary with a real client, a real upstream round trip and real settlement — because an
invariant that holds in-process and breaks behind a web framework is not an invariant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from index import CEILINGS, DEMO_MODEL, app

client = TestClient(app)


def body(key: str = "demo-tight", model: str = DEMO_MODEL, max_tokens: int = 256) -> dict:
    return {
        "model": model,
        "budget_key": key,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": "hello"}],
    }


@pytest.fixture(autouse=True)
def _reset() -> None:
    """Clear demo spend and the rate-limit window before each test.

    The rate limiter is per-client-host with a 120/60s window, and the suite issues well over
    120 requests. Without clearing it, later tests get 429s whose bodies have none of the
    expected keys — which reads as "the endpoint is broken" rather than "the harness tripped
    its own limiter". The limiter itself is tested explicitly instead.
    """
    import index as api

    api._hits.clear()
    client.post("/demo/reset")


class TestTheCeilingHoldsOverHTTP:
    def test_spending_stops_at_the_ceiling_not_after_it(self) -> None:
        ceiling = CEILINGS["demo-tight"]
        allowed = 0
        for _ in range(20):
            if client.post("/v1/chat/completions", json=body()).status_code == 200:
                allowed += 1
        spent = client.get("/v1/budgets/demo-tight").json()["spent_usd"]

        assert allowed > 0, "the demo must admit some requests or it proves nothing"
        assert spent <= ceiling + 1e-9, f"spent {spent} exceeds ceiling {ceiling}"

    def test_refusal_happens_before_the_spend(self) -> None:
        """A 429 must not move the ledger. Refusing after charging is the bug being fixed."""
        for _ in range(20):
            client.post("/v1/chat/completions", json=body())

        before = client.get("/v1/budgets/demo-tight").json()["spent_usd"]
        r = client.post("/v1/chat/completions", json=body())
        after = client.get("/v1/budgets/demo-tight").json()["spent_usd"]

        assert r.status_code == 429
        assert after == before, "a refused request changed recorded spend"

    def test_settlement_releases_the_unused_reservation(self) -> None:
        """A conservative reservation must not permanently consume budget it did not use."""
        r = client.post("/v1/chat/completions", json=body(key="demo-generous"))
        assert r.status_code == 200
        reserved = float(r.headers["x-fusegrid-reserved-usd"])
        actual = float(r.headers["x-fusegrid-actual-usd"])
        released = float(r.headers["x-fusegrid-released-usd"])

        assert actual < reserved, "the stub must underspend, or this test is vacuous"
        assert released == pytest.approx(reserved - actual, abs=1e-6)
        spent = client.get("/v1/budgets/demo-generous").json()["spent_usd"]
        assert spent == pytest.approx(actual, abs=1e-6)


class TestErrorsAreDistinguishable:
    def test_unknown_budget_key_is_400_not_429(self) -> None:
        """A missing key is a configuration error, not an exhausted budget.

        Returning 429 with "only $0.000000 of $0.00 remains" sends whoever is debugging to look
        at their spend instead of their key name. Found by wiring this deployment.
        """
        r = client.post("/v1/chat/completions", json=body(key="no-such-budget"))
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "unknown_budget_key"

    def test_exhausted_budget_is_429(self) -> None:
        for _ in range(20):
            client.post("/v1/chat/completions", json=body())
        r = client.post("/v1/chat/completions", json=body())
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "budget_exceeded"

    def test_unpriced_model_is_refused_never_charged_at_zero(self) -> None:
        before = client.get("/v1/budgets/demo").json()["spent_usd"]
        r = client.post("/v1/chat/completions", json=body(key="demo", model="model-that-is-new"))
        after = client.get("/v1/budgets/demo").json()["spent_usd"]

        assert r.status_code == 429
        assert r.json()["error"]["code"] == "unpriced_model"
        assert after == before


class TestPricingIsCalibrated:
    def test_the_first_request_is_not_refused(self) -> None:
        """A demo whose ceiling admits nothing is indistinguishable from a broken proxy.

        The first pricing attempt made one 256-token request cost $3.94 against a $0.05
        ceiling, so every request was refused. This asserts the calibration holds.
        """
        assert client.post("/v1/chat/completions", json=body()).status_code == 200

    def test_the_tight_budget_admits_a_handful_not_hundreds(self) -> None:
        allowed = sum(
            1
            for _ in range(40)
            if client.post("/v1/chat/completions", json=body()).status_code == 200
        )
        assert 2 <= allowed <= 12, f"admitted {allowed}; recalibrate PRICES or CEILINGS"


class TestOverrunDemo:
    def test_post_hoc_overruns_and_enforced_does_not(self) -> None:
        d = client.get("/demo/overrun?concurrency=20").json()
        assert d["post_hoc"]["overrun_percent"] > 0, "the baseline must fail, or there is no result"
        assert d["enforced"]["overrun_percent"] == 0
        assert d["enforced"]["allowed"] == d["calls_the_ceiling_allows"]

    def test_concurrency_is_capped_so_the_demo_cannot_load_the_instance(self) -> None:
        assert client.get("/demo/overrun?concurrency=100000").json()["concurrency"] <= 60


class TestRateLimit:
    def test_the_limiter_trips_and_says_so(self) -> None:
        """Tested explicitly, since the reset fixture clears it for every other test."""
        import index as api

        api._hits.clear()
        codes = [client.get("/health").status_code for _ in range(130)]
        assert 429 in codes, "the limiter never tripped in 130 requests"
        first_429 = codes.index(429)
        assert first_429 >= 100, f"tripped at request {first_429}, expected around 120"
        api._hits.clear()


class TestHealth:
    def test_health_asserts_the_invariant(self) -> None:
        d = client.get("/health").json()
        assert d["status"] == "ok"
        assert d["invariant_holds"] is True
        assert d["violations"] == []

    def test_health_reports_degraded_when_the_invariant_breaks(self) -> None:
        """A health check that cannot fail is decoration."""
        import index as api

        original = api._ledger.spent
        api._ledger.spent = lambda key: 999.0  # type: ignore[method-assign]
        try:
            d = client.get("/health").json()
            assert d["status"] == "degraded"
            assert d["invariant_holds"] is False
        finally:
            api._ledger.spent = original  # type: ignore[method-assign]


class TestReset:
    def test_reset_can_only_reduce_recorded_spend(self) -> None:
        """The only mutating endpoint. It must not be able to raise or bypass a ceiling."""
        client.post("/v1/chat/completions", json=body())
        assert client.get("/v1/budgets/demo-tight").json()["spent_usd"] > 0

        d = client.post("/demo/reset").json()
        assert d["budgets"] == CEILINGS, "reset must not change any ceiling"
        assert client.get("/v1/budgets/demo-tight").json()["spent_usd"] == 0
        assert client.get("/v1/budgets/demo-tight").json()["ceiling_usd"] == CEILINGS["demo-tight"]
