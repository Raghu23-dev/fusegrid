"""OpenAI-compatible enforcing proxy.

Sits in front of a provider or gateway. A client changes its base URL and nothing else.

WHY A SIDECAR RATHER THAN A LIBRARY

A library must be adopted per service and can be bypassed by any code that does not
import it — including code written before the library existed, and code written by
someone who did not know it existed. A proxy cannot be bypassed by code that is unaware
of it, which is the only kind of enforcement worth the name.

THE ORDER OF OPERATIONS IS THE WHOLE DESIGN

    price → reserve → call upstream → settle

Every failure measured in `bench/baseline/failopen.py` comes from doing some part of
that after the upstream call. Cost cannot be un-spent, so the reservation must happen
first even though the true cost is not yet known — which is why the *maximum* is
reserved rather than an estimate.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .ledger import Ledger, LedgerUnavailable
from .pricing import Pricing, UnpricedModel

log = logging.getLogger("fusegrid")


@dataclass(frozen=True, slots=True)
class Config:
    upstream_base_url: str
    upstream_api_key: str | None = None
    request_timeout_seconds: float = 600.0
    # Fallback when a request does not specify max_tokens. Reserving something is
    # mandatory, so a default is required — but it is explicit config, never a guess
    # derived from the prompt.
    default_max_output_tokens: int = 4096


def estimate_input_tokens(payload: dict[str, Any]) -> int:
    """Approximate input tokens from the request body.

    Deliberately crude — 4 characters per token — and deliberately biased HIGH by
    counting the serialised JSON rather than message content alone.

    Precision is not the goal. This feeds a *reservation*, and a reservation that is
    too large is released at settlement, while one that is too small lets a request
    through that should not have passed. Given a choice between the two errors, only
    one is recoverable.
    """
    try:
        return max(1, len(json.dumps(payload)) // 4)
    except (TypeError, ValueError):
        return 1


def create_app(
    ledger: Ledger,
    pricing: Pricing,
    config: Config,
    *,
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build the proxy.

    `client` exists so a caller can supply their own transport — a stub upstream for a live
    demo, a recording transport for a test, or a client with different connection limits. The
    default is unchanged, so no existing caller is affected.

    Injecting the client rather than the base URL keeps `Config` frozen and declarative, and
    means the proxy under test is the proxy that ships: the request still goes through the
    full client/response cycle instead of a shortcut for the caller's convenience.
    """
    app = FastAPI(title="fusegrid", version="0.1.0")
    client = client or httpx.AsyncClient(timeout=config.request_timeout_seconds)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "open_reservations": ledger.open_reservations}

    @app.get("/v1/budgets/{key}")
    async def budget(key: str) -> JSONResponse:
        ceiling = ledger.ceiling(key)
        if ceiling is None:
            return JSONResponse({"error": "unknown_budget_key", "key": key}, status_code=404)
        try:
            spent = ledger.spent(key)
        except LedgerUnavailable as exc:
            return JSONResponse({"error": "ledger_unavailable", "detail": str(exc)}, 503)
        return JSONResponse(
            {
                "key": key,
                "ceiling_usd": ceiling,
                "spent_usd": spent,
                "remaining_usd": ceiling - spent,
            }
        )

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> Any:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return _error("invalid_request", "body must be JSON", 400)

        if not isinstance(payload, dict):
            return _error("invalid_request", "body must be a JSON object", 400)

        budget_key = request.headers.get("x-fusegrid-budget", "default")
        model = payload.get("model")
        if not isinstance(model, str) or not model:
            return _error("invalid_request", "model is required", 400)

        # 1. Price the request. An unknown model has no maximum, so nothing can be
        #    reserved, so it cannot be admitted. This is measured failure #4 — a
        #    default price would silently mis-enforce for the newest models.
        max_output = payload.get("max_tokens")
        # A negative max_tokens is a CALLER ERROR, rejected before pricing. Left to the
        # pricing layer it raised from library code and would surface as a 500; more
        # importantly, it used to be priced as a credit and served free past an exhausted
        # ceiling. `isinstance(True, int)` is True in Python, so bools are excluded
        # explicitly — `max_tokens: true` is not a token count.
        if max_output is not None and (
            isinstance(max_output, bool) or not isinstance(max_output, int) or max_output < 0
        ):
            log.warning("denied: invalid max_tokens %r (budget=%s)", max_output, budget_key)
            return _error(
                "invalid_request",
                f"max_tokens must be a non-negative integer, got {max_output!r}. A negative "
                "value would price the request as a credit and bypass the ceiling.",
                400,
            )
        try:
            max_cost = pricing.max_cost(
                model,
                estimate_input_tokens(payload),
                max_output if isinstance(max_output, int) else config.default_max_output_tokens,
            )
        except UnpricedModel as exc:
            log.warning("denied: unpriced model %s (budget=%s)", model, budget_key)
            return _error(
                "unpriced_model",
                str(exc),
                429,
                extra={"model": model, "known_models": pricing.known_models()},
            )

        # 2. Reserve BEFORE calling upstream. A store failure denies — fail closed.
        try:
            decision = ledger.reserve(budget_key, max_cost)
        except LedgerUnavailable as exc:
            log.error("denied: ledger unavailable (budget=%s): %s", budget_key, exc)
            return _error(
                "ledger_unavailable",
                "the spend ledger is unreachable, so the budget cannot be enforced. "
                "fusegrid denies rather than allowing unenforced spend.",
                503,
            )

        # An unknown key is a CALLER ERROR, not an exhausted budget. Returning it as 429
        # with "only $0.000000 of $0.00 remains" is indistinguishable from a genuinely
        # spent-out budget, and sends whoever is debugging to look at their spend instead of
        # their key name. Found by wiring the live deployment, where a typo'd budget_key
        # produced a message describing a ceiling that does not exist.
        if decision.reason == "unknown_budget_key":
            log.warning("denied: unknown budget key %s", budget_key)
            return _error(
                "unknown_budget_key",
                f"no budget is configured for key {budget_key!r}. This is a configuration "
                "error, not an exhausted budget: no ceiling exists to enforce, so the "
                "request is refused rather than admitted unenforced.",
                400,
                extra={"budget_key": budget_key},
            )

        if not decision.allowed or decision.reservation is None:
            log.info(
                "denied: %s (budget=%s, remaining=%.6f)",
                decision.reason,
                budget_key,
                decision.remaining,
            )
            return _error(
                decision.reason or "budget_exceeded",
                f"request would cost up to ${max_cost:.6f} but only "
                f"${decision.remaining:.6f} of ${decision.ceiling:.2f} remains",
                429,
                extra={
                    "reserved_usd": round(max_cost, 6),
                    "remaining_usd": round(decision.remaining, 6),
                    "ceiling_usd": decision.ceiling,
                },
            )

        reservation = decision.reservation
        streaming = bool(payload.get("stream"))
        headers = {"content-type": "application/json"}
        if config.upstream_api_key:
            headers["authorization"] = f"Bearer {config.upstream_api_key}"

        url = f"{config.upstream_base_url.rstrip('/')}/v1/chat/completions"

        # 3. Call upstream. Any failure releases the reservation in full — a request
        #    that never reached the provider was never billed.
        if streaming:
            return StreamingResponse(
                _stream(client, url, headers, payload, ledger, pricing, model, reservation.id),
                media_type="text/event-stream",
                headers={
                    "cache-control": "no-cache",
                    "x-fusegrid-reserved-usd": f"{max_cost:.6f}",
                },
            )

        try:
            upstream = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            ledger.release(reservation.id)
            log.warning("upstream error, reservation released: %s", exc)
            return _error("upstream_error", str(exc), 502)

        if upstream.status_code >= 400:
            # The provider rejected it, so nothing was billed.
            ledger.release(reservation.id)
            return JSONResponse(_safe_json(upstream), status_code=upstream.status_code)

        body = _safe_json(upstream)
        actual = _actual_cost(pricing, model, body)
        released = _settle(ledger, reservation.id, actual)

        return JSONResponse(
            body,
            headers={
                "x-fusegrid-reserved-usd": f"{max_cost:.6f}",
                "x-fusegrid-actual-usd": f"{actual:.6f}",
                "x-fusegrid-released-usd": f"{released:.6f}",
            },
        )

    return app


async def _stream(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    ledger: Ledger,
    pricing: Pricing,
    model: str,
    reservation_id: str,
) -> AsyncIterator[bytes]:
    """Proxy a stream untouched, settling from the final usage chunk.

    Bytes pass through unbuffered: buffering to count tokens would destroy
    time-to-first-token, which is the only reason to stream in the first place.

    The `finally` block is load-bearing. A client that disconnects mid-stream would
    otherwise leave the reservation held until the sweeper reclaims it, slowly starving
    the budget.
    """
    settled = False
    try:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code >= 400:
                await response.aread()
                ledger.release(reservation_id)
                settled = True
                yield b'data: {"error":"upstream_error"}\n\n'
                return

            async for chunk in response.aiter_bytes():
                yield chunk
                # Usage arrives in the final chunk when the caller requests it. Parsed
                # opportunistically: a stream that never reports usage settles at the
                # reserved amount, which over-charges rather than under-charges — the
                # safe direction.
                usage = _usage_from_chunk(chunk)
                if usage is not None and not settled:
                    _settle(ledger, reservation_id, _cost_from_usage(pricing, model, usage))
                    settled = True
    except (httpx.HTTPError, GeneratorExit):
        if not settled:
            ledger.release(reservation_id)
            settled = True
        raise
    finally:
        if not settled:
            # No usage reported: settle at the full reservation rather than releasing
            # it, because the provider did serve the request.
            with contextlib.suppress(KeyError):
                ledger.settle(reservation_id, float("inf"))


def _usage_from_chunk(chunk: bytes) -> dict[str, Any] | None:
    for line in chunk.split(b"\n"):
        if not line.startswith(b"data:"):
            continue
        raw = line[5:].strip()
        if raw in (b"", b"[DONE]"):
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        usage = parsed.get("usage") if isinstance(parsed, dict) else None
        if isinstance(usage, dict) and usage.get("total_tokens") is not None:
            return usage
    return None


def _cost_from_usage(pricing: Pricing, model: str, usage: dict[str, Any]) -> float:
    """Actual cost from a usage block, or infinity if it cannot be trusted.

    An EMPTY or token-less usage block is not "zero cost" — it is missing data. Costing
    it at zero would release the entire reservation for a request the provider did
    serve and will bill, which is measured failure #4 in a different disguise. Returning
    infinity settles at the reservation instead: over-charging is recoverable at the next
    window reset, under-charging is not recoverable at all.
    """
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None and completion is None:
        return float("inf")
    try:
        return pricing.actual_cost(model, int(prompt or 0), int(completion or 0))
    except (UnpricedModel, TypeError, ValueError):
        # InvalidTokenCount is a ValueError, so a usage block reporting NEGATIVE tokens
        # lands here and settles at the full reservation. That is the safe direction: a
        # provider (or a compromised upstream) reporting -1000 completion tokens must not
        # be able to hand budget back. Untrustworthy usage is missing usage.
        return float("inf")


def _actual_cost(pricing: Pricing, model: str, body: dict[str, Any]) -> float:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return float("inf")
    return _cost_from_usage(pricing, model, usage)


def _settle(ledger: Ledger, reservation_id: str, actual: float) -> float:
    try:
        return ledger.settle(reservation_id, actual)
    except KeyError:
        return 0.0
    except LedgerUnavailable as exc:
        # A failed settle leaves the reservation held, which over-charges the budget
        # rather than under-charging it. The ceiling still holds; the key just has less
        # headroom until the window resets. Logged loudly because it is silent otherwise.
        log.error("settle failed, reservation remains held: %s", exc)
        return 0.0


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed = response.json()
    except (json.JSONDecodeError, ValueError):
        return {"error": "upstream returned non-JSON", "status": response.status_code}
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def _error(
    code: str, message: str, status: int, extra: dict[str, Any] | None = None
) -> JSONResponse:
    """Machine-readable denial. Criterion 2.

    `code` is stable and enumerable so a client can branch on it; `message` is for a
    human reading a log. Mixing the two into one string forces callers to parse prose.
    """
    body: dict[str, Any] = {"error": {"code": code, "message": message, "type": "fusegrid"}}
    if extra:
        body["error"].update(extra)
    return JSONResponse(body, status_code=status)
