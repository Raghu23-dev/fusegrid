"""ADVERSARIAL: try to get spend past the proxy.

The proxy is where enforcement becomes unbypassable, so these tests attack the HTTP
surface rather than the ledger directly. A fake upstream stands in for a provider, so
every test runs offline in milliseconds.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from fusegrid import Ledger, MemoryStore, ModelPrice, Pricing
from fusegrid.proxy import Config, create_app

MODEL = "test-model"
KEY = "team-a"
CEILING = 1.00


_DEFAULT_USAGE = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}


def build(
    monkeypatch: pytest.MonkeyPatch,
    *,
    upstream_status: int = 200,
    usage: dict[str, int] | None = None,
    fail: bool = False,
) -> tuple[TestClient, MemoryStore, Ledger]:
    store = MemoryStore()
    ledger = Ledger(store, {KEY: CEILING})
    pricing = Pricing({MODEL: ModelPrice(input_per_mtok=1000.0, output_per_mtok=1000.0)})

    body: dict[str, Any] = {
        "id": "x",
        "choices": [{"message": {"content": "hi"}}],
        # `usage if usage is not None` rather than `usage or ...`: an EMPTY dict is a
        # deliberate test case (a provider that reported no usage) and `or` would
        # silently replace it with the default, which hid a real bug once already.
        "usage": _DEFAULT_USAGE if usage is None else usage,
    }

    async def fake_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("upstream down")
        return httpx.Response(upstream_status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    app = create_app(ledger, pricing, Config(upstream_base_url="http://upstream.test"))
    return TestClient(app), store, ledger


def ask(
    client: TestClient, *, model: str = MODEL, max_tokens: int = 100, key: str = KEY
) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"x-fusegrid-budget": key},
    )


class TestEnforcementCannotBeBypassed:
    def test_request_within_budget_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _, _ = build(monkeypatch)
        assert ask(client, max_tokens=1).status_code == 200

    def test_request_over_budget_is_denied_with_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, store, _ = build(monkeypatch)
        # Prices are set so a large max_tokens exceeds the ceiling on its own.
        r = ask(client, max_tokens=10_000)
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "budget_exceeded"
        assert store.spent(KEY) == pytest.approx(0.0)

    def test_denial_is_machine_readable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Criterion 2: a client must be able to branch on the reason."""
        client, _, _ = build(monkeypatch)
        err = ask(client, max_tokens=10_000).json()["error"]
        assert err["code"] == "budget_exceeded"
        assert "remaining_usd" in err and "ceiling_usd" in err and "reserved_usd" in err
        assert isinstance(err["message"], str) and err["message"]

    def test_repeated_requests_stop_at_the_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, store, _ = build(
            monkeypatch,
            usage={"prompt_tokens": 500, "completion_tokens": 500, "total_tokens": 1000},
        )
        allowed = sum(ask(client, max_tokens=400).status_code == 200 for _ in range(40))
        assert allowed < 40, "some requests must be denied"
        assert store.spent(KEY) <= CEILING + 1e-6

    def test_unpriced_model_is_denied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, store, _ = build(monkeypatch)
        r = ask(client, model="model-nobody-configured")
        assert r.status_code == 429
        err = r.json()["error"]
        assert err["code"] == "unpriced_model"
        # The error must help: it lists what IS configured.
        assert MODEL in err["known_models"]
        assert store.spent(KEY) == pytest.approx(0.0)

    def test_unknown_budget_key_is_denied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _, _ = build(monkeypatch)
        r = ask(client, key="no-such-team", max_tokens=1)
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "unknown_budget_key"

    def test_ledger_failure_denies_with_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail closed: the store is down, so spend cannot be enforced, so deny."""
        client, store, _ = build(monkeypatch)
        store.fail = True
        r = ask(client, max_tokens=1)
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "ledger_unavailable"


class TestReservationLifecycle:
    def test_settlement_releases_the_unused_reservation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, store, ledger = build(monkeypatch)
        r = ask(client, max_tokens=200)
        assert r.status_code == 200
        reserved = float(r.headers["x-fusegrid-reserved-usd"])
        actual = float(r.headers["x-fusegrid-actual-usd"])
        released = float(r.headers["x-fusegrid-released-usd"])
        assert actual < reserved
        assert released == pytest.approx(reserved - actual, abs=1e-5)
        assert store.spent(KEY) == pytest.approx(actual, abs=1e-5)
        assert ledger.open_reservations == 0

    def test_upstream_error_releases_the_whole_reservation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A request that never reached the provider was never billed."""
        client, store, ledger = build(monkeypatch, fail=True)
        assert ask(client, max_tokens=100).status_code == 502
        assert store.spent(KEY) == pytest.approx(0.0)
        assert ledger.open_reservations == 0

    def test_upstream_4xx_releases_the_reservation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, store, ledger = build(monkeypatch, upstream_status=400)
        assert ask(client, max_tokens=100).status_code == 400
        assert store.spent(KEY) == pytest.approx(0.0)
        assert ledger.open_reservations == 0

    def test_missing_usage_settles_at_the_reservation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No usage reported → charge the reservation. Over-charging is the safe direction."""
        client, store, _ = build(monkeypatch, usage={})
        r = ask(client, max_tokens=100)
        assert r.status_code == 200
        reserved = float(r.headers["x-fusegrid-reserved-usd"])
        assert store.spent(KEY) == pytest.approx(reserved, abs=1e-5)


class TestHostileInput:
    def test_non_json_body_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _, _ = build(monkeypatch)
        r = client.post(
            "/v1/chat/completions",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400

    def test_json_array_body_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _, _ = build(monkeypatch)
        assert client.post("/v1/chat/completions", json=[1, 2, 3]).status_code == 400

    def test_missing_model_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _, _ = build(monkeypatch)
        r = client.post("/v1/chat/completions", json={"messages": []})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_request"

    def test_absent_budget_header_uses_default_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No header must not mean 'no budget'."""
        client, _, _ = build(monkeypatch)
        r = client.post(
            "/v1/chat/completions", json={"model": MODEL, "max_tokens": 1, "messages": []}
        )
        # 'default' is not a configured key, so it is denied rather than unlimited.
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "unknown_budget_key"

    def test_input_estimate_is_biased_high(self) -> None:
        from fusegrid.proxy import estimate_input_tokens

        small = estimate_input_tokens(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        )
        big = estimate_input_tokens(
            {"model": "m", "messages": [{"role": "user", "content": "x" * 4000}]}
        )
        assert big > small
        assert small >= 1  # never zero, which would reserve nothing for input


class TestObservability:
    def test_health_reports_open_reservations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _, _ = build(monkeypatch)
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["open_reservations"] == 0

    def test_budget_endpoint_reports_remaining(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _, _ = build(monkeypatch)
        ask(client, max_tokens=1)
        body = client.get(f"/v1/budgets/{KEY}").json()
        assert body["ceiling_usd"] == pytest.approx(CEILING)
        assert body["remaining_usd"] < CEILING

    def test_unknown_budget_returns_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, _, _ = build(monkeypatch)
        assert client.get("/v1/budgets/nope").status_code == 404
