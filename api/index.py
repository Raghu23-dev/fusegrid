"""Live instance of fusegrid.

WHY A LIVE INSTANCE OF A PROXY WITH NO REAL UPSTREAM

The thing fusegrid claims is not "it forwards requests to an LLM" — anything does that. It
claims that a budget ceiling **holds**, atomically, under concurrency, where the four common
post-hoc patterns fail open by 95-100%.

Demonstrating that needs a ledger, a pricing table and concurrent load. It does not need a real
model, and wiring one in would mean either publishing a credential or asking a visitor for
theirs. So the upstream is a stub that returns a deterministic usage block: the reserve → call →
settle path runs in full, and every number a visitor sees is the real ledger's.

WHAT A VISITOR CAN DO

- `POST /v1/chat/completions` against a per-visitor budget and watch it get refused at the
  ceiling rather than after it.
- `GET /demo/overrun` to run the post-hoc baseline and the enforced path side by side, and see
  95-100% overrun become 0%.

Nothing here reads a secret. There are no environment variables to set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fusegrid import Ledger, MemoryStore, ModelPrice, Pricing, RedisStore
from fusegrid.proxy import Config, create_app

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)
log = logging.getLogger("fusegrid")

VERSION = "0.1.0"

#: Priced so that a visitor reaches the ceiling in a handful of requests. Real frontier pricing
#: would need thousands of calls to demonstrate anything.
#:
#: CALIBRATED, not picked. The first attempt used $3,000/$15,000 per Mtok, which made a single
#: 256-token request cost $3.94 against a $0.05 ceiling — so EVERY request was refused and the
#: demo showed a ceiling that admits nothing rather than one that holds. A refusal on request 1
#: is indistinguishable from a broken proxy.
#:
#: These numbers give a max reservation of about $0.0104 per default request, so roughly 5 fit
#: in the "demo-tight" budget of $0.05 and about 48 in "demo" at $0.50.
DEMO_MODEL = "demo-expensive-1"
PRICES = {
    DEMO_MODEL: ModelPrice(input_per_mtok=20.0, output_per_mtok=40.0),
    "demo-cheap-1": ModelPrice(input_per_mtok=1.0, output_per_mtok=2.0),
}

#: Per-visitor ceilings. Deliberately small so the interesting behaviour — refusal at the
#: boundary — happens within a few requests rather than a few thousand.
CEILINGS = {
    "demo": 0.50,
    "demo-tight": 0.05,
    "demo-generous": 5.00,
}


def _build_store() -> tuple[Any, str]:
    """Redis if it is configured, memory only if it is genuinely absent.

    WHY THIS IS NOT A PREFERENCE. The first deployment of this service used MemoryStore, and
    real-world scenario testing found it overrunning its ceiling by 303%: 25 concurrent requests
    were served by 25 serverless instances, each holding its own ledger, each starting empty.
    Every instance enforced $0.05 correctly against its own slice of traffic, so there were 25
    ceilings instead of one.

    `store.py` had said so all along — "correct for one replica and useless for two, which is
    precisely measured failure #1" — and `docs/DEPLOYMENT.md` said not to deploy it behind a
    load balancer. I deployed it anyway and invited people to verify a property it did not have.

    So the store is chosen at startup and the choice is REPORTED at /health, because a service
    whose central guarantee depends on its backing store should not make that invisible.
    """
    url = os.environ.get("REDIS_URL") or os.environ.get("KV_URL")
    if not url:
        return MemoryStore(), "memory"
    try:
        import redis

        client = redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
        client.ping()
        return RedisStore(client), "redis"
    except Exception as exc:
        # Deliberately does NOT silently fall back to memory. A shared ledger that quietly
        # degrades to a per-instance one is how the 303% overrun happened in the first place;
        # the failure has to be loud. The service still starts, so /health can report it.
        log.error("redis configured but unreachable: %s", exc)
        return MemoryStore(), f"memory (redis configured but UNREACHABLE: {exc})"


_store, _store_kind = _build_store()
_ledger = Ledger(_store, CEILINGS)
_pricing = Pricing(PRICES)
log.info("ledger store: %s", _store_kind)

# The stub upstream. Mounted on this same app, so the proxy makes a real HTTP round trip
# through its real client — the reserve/settle path is not shortcut for the demo.
_stub = FastAPI()


@_stub.post("/v1/chat/completions")
async def stub_completions(request: Request) -> dict[str, Any]:
    """A deterministic upstream.

    Returns a usage block derived from the request so cost is reproducible: a visitor who
    repeats a request gets the same charge and can reason about the ledger.
    """
    # Registered at /v1/chat/completions because the proxy appends that path to the configured
    # base URL. Mounting it at /chat/completions gave a 404 that the proxy correctly reported as
    # an upstream 4xx — and correctly released the reservation for, so no budget was consumed by
    # my own misconfiguration. Worth noting: the failure mode was safe.
    body = await request.json()
    # max(0, ...) because a negative max_tokens made completion_tokens negative, which the
    # pricing layer treated as a credit — the request was served for $0.000000 past an
    # exhausted ceiling. The proxy now rejects negatives before reaching here; this stays
    # so the stub cannot manufacture a negative usage block on its own.
    max_out = max(0, int(body.get("max_tokens") or 256))
    # Spend 80% of the reservation. Not 100%, so settlement visibly releases the difference —
    # which is the part of the design that stops a conservative reservation from permanently
    # consuming budget it never used.
    return {
        "id": "stub-1",
        "object": "chat.completion",
        "model": body.get("model", DEMO_MODEL),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "(stub upstream response)"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": max(1, len(str(body.get("messages", ""))) // 4),
            "completion_tokens": int(max_out * 0.8),
            "total_tokens": 0,
        },
    }


def _build_proxy() -> FastAPI:
    """Wire the real proxy to the stub upstream.

    `create_app` takes an optional client so a caller can supply their own transport. Here it
    is an ASGI transport pointed at the stub, so the proxy performs a full request/response
    cycle through its normal code path — the deployed behaviour is the tested behaviour.
    """
    return create_app(
        _ledger,
        _pricing,
        Config(upstream_base_url="http://stub", default_max_output_tokens=256),
        client=httpx.AsyncClient(transport=httpx.ASGITransport(app=_stub), timeout=30.0),
    )


_proxy_app = _build_proxy()

app = FastAPI(title="fusegrid", version=VERSION, docs_url="/docs")

_RATE_WINDOW_S = 60
_RATE_MAX = 120
_hits: dict[str, deque[float]] = {}


def _rate_limited(key: str) -> bool:
    now = time.time()
    window = _hits.setdefault(key, deque())
    while window and now - window[0] > _RATE_WINDOW_S:
        window.popleft()
    if len(window) >= _RATE_MAX:
        return True
    window.append(now)
    return False


@app.middleware("http")
async def guard(request: Request, call_next: Any) -> Any:
    start = time.perf_counter()
    who = request.client.host if request.client else "-"
    if _rate_limited(who):
        return JSONResponse({"error": "rate_limited", "limit": f"{_RATE_MAX}/60s"}, 429)
    try:
        response = await call_next(request)
    except Exception:
        # An error message that varies with ledger internals would disclose more than it helps.
        log.exception("unhandled")
        return JSONResponse({"error": "internal_error"}, 500)
    ms = (time.perf_counter() - start) * 1000
    log.info("%s %s -> %s in %.1fms", request.method, request.url.path, response.status_code, ms)
    response.headers["X-Response-Time-Ms"] = f"{ms:.1f}"
    response.headers["X-Fusegrid-Version"] = VERSION
    return response


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness plus the invariant.

    `committed + open reservations <= ceiling` is the property the whole project exists to
    hold. A health check that returns 200 without testing it would be decoration.
    """
    violations = []
    for key, ceiling in CEILINGS.items():
        try:
            spent = _ledger.spent(key)
        except Exception as exc:
            violations.append(f"{key}: ledger unavailable ({exc})")
            continue
        if spent > ceiling + 1e-9:
            violations.append(f"{key}: spent {spent} exceeds ceiling {ceiling}")

    shared = _store_kind == "redis"
    if not shared:
        violations.append(
            f"ledger store is {_store_kind!r}, not shared across instances — the ceiling holds "
            "per instance, not globally. See scenarios/results/2026-08-21-fusegrid-multi-instance.md"
        )

    return {
        "status": "ok" if not violations else "degraded",
        "version": VERSION,
        "invariant": "committed + open reservations <= ceiling",
        "invariant_holds": not violations,
        "ledger_store": _store_kind,
        "ledger_shared_across_instances": shared,
        "instance": os.environ.get("VERCEL_DEPLOYMENT_ID", "local")[-12:],
        "violations": violations,
        "open_reservations": _ledger.open_reservations,
        "budgets": {
            k: {"ceiling_usd": v, "spent_usd": round(_ledger.spent(k), 6)}
            for k, v in CEILINGS.items()
        },
        "commit": os.environ.get("VERCEL_GIT_COMMIT_SHA", "local")[:7],
    }


@app.get("/demo/overrun")
async def demo_overrun(concurrency: int = 20) -> dict[str, Any]:
    """Post-hoc accounting vs reserve-then-settle, measured live on this instance.

    Runs the same burst twice: once with the ledger consulted AFTER the call (how most budget
    code is written), once with it consulted BEFORE. Reports the overrun of each.

    This is `bench/enforce/replay.py` reduced to one request, so a visitor does not have to
    clone the repo to see the result.
    """
    concurrency = max(2, min(concurrency, 60))
    ceiling = 0.10
    cost_per_call = 0.02  # 5 calls fit; the rest must be refused

    # ── post-hoc: check the budget after spending ──
    posthoc_store = MemoryStore()
    posthoc_ledger = Ledger(posthoc_store, {"k": ceiling})
    posthoc_spent = 0.0
    lock = asyncio.Lock()

    async def posthoc_call() -> None:
        nonlocal posthoc_spent
        # The pattern: read, decide, spend. Between the read and the spend, every other
        # concurrent request reads the same stale value.
        if posthoc_ledger.spent("k") >= ceiling:
            return
        await asyncio.sleep(0.001)  # the upstream call
        async with lock:
            posthoc_spent += cost_per_call
        posthoc_store.settle("k", 0.0, cost_per_call)

    await asyncio.gather(*(posthoc_call() for _ in range(concurrency)))

    # ── enforced: reserve before calling ──
    enforced_store = MemoryStore()
    enforced_ledger = Ledger(enforced_store, {"k": ceiling})
    refused = 0
    allowed = 0

    async def enforced_call() -> None:
        nonlocal refused, allowed
        decision = enforced_ledger.reserve("k", cost_per_call)
        # Check the reservation, not just the flag. `allowed` being true does not prove
        # `reservation` is present, and the proxy's own handler tests both for that reason —
        # this demo endpoint tested only the flag and would have raised AttributeError on any
        # path that returned allowed without one. Found by widening mypy past src/.
        if not decision.allowed or decision.reservation is None:
            refused += 1
            return
        await asyncio.sleep(0.001)
        enforced_ledger.settle(decision.reservation.id, cost_per_call)
        allowed += 1

    await asyncio.gather(*(enforced_call() for _ in range(concurrency)))

    enforced_spent = enforced_ledger.spent("k")

    def over(spent: float) -> float:
        return round(max(0.0, (spent - ceiling) / ceiling) * 100, 1)

    return {
        "concurrency": concurrency,
        "ceiling_usd": ceiling,
        "cost_per_call_usd": cost_per_call,
        "calls_the_ceiling_allows": int(ceiling / cost_per_call),
        "post_hoc": {
            "spent_usd": round(posthoc_spent, 6),
            "overrun_percent": over(posthoc_spent),
            "why": (
                "The budget is read before the call and written after it. Every concurrent "
                "request reads the same value before any of them has written, so all of them "
                "are permitted."
            ),
        },
        "enforced": {
            "spent_usd": round(enforced_spent, 6),
            "overrun_percent": over(enforced_spent),
            "allowed": allowed,
            "refused": refused,
            "why": (
                "The reservation is taken atomically before the call. The ceiling is a "
                "precondition, not a report."
            ),
        },
        "note": (
            "Reproduces bench/enforce/replay.py against this running instance. Concurrency is "
            "capped at 60 so the demo cannot be used to load the deployment."
        ),
        "scope": (
            "Both arms run IN THIS INSTANCE, so this measures the algorithm and not the "
            "deployment. Whether the ceiling holds across instances depends on the ledger "
            f"store, which is currently {_store_kind!r}. Real-world scenario testing found a "
            "303% overrun when this ran on a per-instance memory store — see "
            "scenarios/results/2026-08-21-fusegrid-multi-instance.md."
        ),
        "ledger_store": _store_kind,
    }


@app.get("/v1/budgets/{key}")
def budget(key: str) -> JSONResponse:
    ceiling = _ledger.ceiling(key)
    if ceiling is None:
        return JSONResponse(
            {"error": "unknown_budget_key", "key": key, "known": sorted(CEILINGS)}, 404
        )
    spent = _ledger.spent(key)
    return JSONResponse(
        {
            "key": key,
            "ceiling_usd": ceiling,
            "spent_usd": round(spent, 6),
            "remaining_usd": round(ceiling - spent, 6),
            "open_reservations": _ledger.open_reservations,
        }
    )


@app.post("/v1/chat/completions")
async def completions(request: Request) -> Any:
    """The proxy path, end to end, against the stub upstream.

    Delegates to the real `create_app` handler so the deployed behaviour is the tested
    behaviour rather than a reimplementation for the demo.
    """
    body = await request.body()
    headers = {
        "content-type": "application/json",
        # The proxy reads the budget key from a HEADER. Accepted in the body too, because a
        # curl example is easier to follow with everything in one JSON object — translated
        # here rather than changing the library's interface for the demo's convenience.
        "x-fusegrid-budget": request.headers.get("x-fusegrid-budget")
        or _budget_key_from_body(body),
    }

    transport = httpx.ASGITransport(app=_proxy_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        response = await client.post(
            "/v1/chat/completions", content=body, headers=headers, timeout=30.0
        )

    passthrough = {k: v for k, v in response.headers.items() if k.lower().startswith("x-fusegrid-")}
    passthrough["X-Upstream"] = "stub"
    return JSONResponse(
        content=_safe(response), status_code=response.status_code, headers=passthrough
    )


def _budget_key_from_body(body: bytes) -> str:
    """Read `budget_key` out of the JSON body, defaulting to the demo budget.

    Defaults to "demo" rather than "default": a key that is not configured now returns 400,
    so silently falling back to an unconfigured name would refuse every request with a
    configuration error and look like the ceiling was broken.
    """
    try:
        parsed = json.loads(body)
        key = parsed.get("budget_key") if isinstance(parsed, dict) else None
        return key if isinstance(key, str) and key else "demo"
    except (json.JSONDecodeError, ValueError):
        return "demo"


def _safe(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"error": "upstream_returned_non_json", "body": response.text[:400]}


@app.post("/demo/reset")
def reset() -> dict[str, Any]:
    """Reset the demo ledger.

    Exists because a shared instance with small ceilings is exhausted by the first visitor, and
    a demo nobody after the first can run is not a demo. It is the only mutating endpoint, and
    it can only ever *reduce* recorded spend — it never touches a ceiling.

    CLEARS THE EXISTING LEDGER IN PLACE rather than rebinding `_ledger`. Rebinding looked
    correct and was not: `create_app` captures the ledger in a closure, so after a reset the
    proxy kept writing to the old ledger while `/v1/budgets` read the new one — spend appeared
    to vanish while the real ceiling silently kept counting. Caught by a test asserting that
    spend recorded through the proxy is visible via the budget endpoint.
    """
    # RedisStore has no clear(); the demo keys are cleared by zeroing each budget instead.
    if hasattr(_store, "clear"):
        _store.clear()
    else:
        for key in CEILINGS:
            try:
                _store.release(key, _store.spent(key))
            except Exception as exc:
                log.warning("could not clear %s: %s", key, exc)
    _ledger.clear_reservations()
    log.info("demo ledger reset")
    return {"status": "reset", "budgets": CEILINGS}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>fusegrid — a budget ceiling that actually holds</title>
<style>
 :root {{ color-scheme: dark }}
 body {{ background:#0b0d10; color:#e7e9ee; font:16px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace;
        margin:0; padding:2.5rem 1.25rem }}
 main {{ max-width:62rem; margin:0 auto }}
 h1 {{ font-size:1.5rem; margin:0 0 .25rem }}
 h2 {{ font-size:1.05rem; margin:2.25rem 0 .6rem; color:#8fb6ff }}
 p.sub {{ color:#788092; margin:0 0 2rem }}
 table {{ border-collapse:collapse; width:100%; margin:.5rem 0 1rem; font-size:.875rem }}
 th,td {{ text-align:left; padding:.4rem .7rem; border-bottom:1px solid #1c212b }}
 th {{ color:#788092; font-weight:400 }}
 code {{ background:#151a21; padding:.1rem .35rem; border-radius:3px; font-size:.875em }}
 pre {{ background:#151a21; padding:.85rem 1rem; border-radius:6px; overflow-x:auto;
        font-size:.8125rem; border:1px solid #1c212b }}
 a {{ color:#8fb6ff }}
 .n {{ color:#788092 }}
</style></head><body><main>
<h1>fusegrid</h1>
<p class="sub">A spend ceiling that holds under concurrency. v{VERSION} &middot;
<a href="/docs">API docs</a> &middot; <a href="/health">health</a></p>

<h2>The result, in one request</h2>
<pre>curl "{{HOST}}/demo/overrun?concurrency=20"</pre>
<p>Runs the same 20-request burst twice against a $0.10 ceiling that permits 5 calls: once with
the budget checked <em>after</em> the call, once with a reservation taken <em>before</em> it.
Post-hoc accounting overruns. Reserve-then-settle refuses the 6th request.</p>

<h2>Or spend a budget down yourself</h2>
<pre>curl -X POST {{HOST}}/v1/chat/completions \\
  -H 'content-type: application/json' \\
  -d '{{"model":"{DEMO_MODEL}","budget_key":"demo-tight",
       "max_tokens":256,"messages":[{{"role":"user","content":"hello"}}]}}'

curl {{HOST}}/v1/budgets/demo-tight     <span class="n"># watch remaining_usd fall</span>
curl -X POST {{HOST}}/demo/reset        <span class="n"># put it back for the next visitor</span></pre>
<p>Keep going and the ceiling refuses you with <code>429</code> and a
<code>budget_exceeded</code> body carrying <code>remaining_usd</code> and
<code>ceiling_usd</code> &mdash; before the spend, not after it.</p>

<h2>Budgets on this instance</h2>
<table><thead><tr><th>budget_key</th><th>ceiling</th></tr></thead><tbody>
{"".join(f"<tr><td><code>{k}</code></td><td>${v:.2f}</td></tr>" for k, v in CEILINGS.items())}
</tbody></table>

<h2>Models</h2>
<table><thead><tr><th>model</th><th>input /Mtok</th><th>output /Mtok</th></tr></thead><tbody>
{"".join(f"<tr><td><code>{m}</code></td><td>${p.input_per_mtok:,.0f}</td><td>${p.output_per_mtok:,.0f}</td></tr>" for m, p in PRICES.items())}
</tbody></table>
<p class="n">Priced far above real models on purpose, so a ceiling is reachable in a handful of
requests rather than a few thousand. An unpriced model is <strong>refused</strong>, never
charged at zero.</p>

<h2>The upstream here is a stub, and that is deliberate</h2>
<p>fusegrid does not claim to forward requests well &mdash; anything does that. It claims a
ceiling <em>holds</em>. Demonstrating that needs a ledger, a pricing table and concurrency; it
does not need a real model, and wiring one in would mean publishing a credential or asking for
yours. The reserve &rarr; call &rarr; settle path runs in full and every number above is the
real ledger's.</p>
</main>
<script>document.body.innerHTML = document.body.innerHTML.replaceAll('{{HOST}}', location.origin);</script>
</body></html>"""
