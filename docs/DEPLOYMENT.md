# Deployment

> **Gate:** "published to a package registry" is not deployment. A stranger must be able to hit
> a running instance.

**Live:** https://fusegrid.vercel.app

The result in one request, no setup:

```bash
curl "https://fusegrid.vercel.app/demo/overrun?concurrency=20"
```

Runs the same 20-request burst twice against a $0.10 ceiling that permits 5 calls — once with the
budget checked *after* the call, once with a reservation taken *before* it.

## Verified in production

```
GET  /demo/overrun?concurrency=20   post-hoc $0.40, 300% over · enforced $0.10, 0% over, 5 allowed / 15 refused
POST /v1/chat/completions ×7        200 200 200 200 200 429 429   — refused at the ceiling, not after
GET  /v1/budgets/demo-tight         remaining fell $0.0417 → $0.0083, then held
GET  /health                        invariant_holds: true
     unknown budget key             400 (configuration error)
     unpriced model                 429 (refused, never charged at zero)
```

## The upstream is a stub, deliberately

fusegrid does not claim to forward requests well — anything does that. It claims a ceiling
**holds** under concurrency, where four common post-hoc patterns fail open by 95–100%.

Demonstrating that needs a ledger, a pricing table and concurrency. It does not need a real
model, and wiring one in would mean either publishing a credential or asking a visitor for
theirs. The stub returns a deterministic usage block; the reserve → call → settle path runs in
full through the real `create_app` handler, and every number a visitor sees is the real ledger's.

`create_app` takes an optional `client`, so the stub is injected as a transport rather than
special-cased. The deployed proxy is the tested proxy.

## Endpoints

| Route | What |
|---|---|
| `GET /` | Landing page with copy-pasteable curls |
| `GET /demo/overrun` | `bench/enforce/replay.py` against this instance, one request |
| `POST /v1/chat/completions` | The proxy. Reserves before calling, settles after |
| `GET /v1/budgets/{key}` | Ceiling, spent, remaining, open reservations |
| `POST /demo/reset` | Clears demo spend. Never touches a ceiling |
| `GET /health` | Asserts the invariant, not liveness |
| `GET /docs` | OpenAPI |

## Operational surface

| Concern | Implementation |
|---|---|
| Health check | `/health` asserts `committed + open reservations ≤ ceiling` on the responding instance, per budget. A test monkeypatches spend to 999.0 and asserts the check reports `degraded` — a health check that cannot fail is decoration. |
| Structured logs | JSON to stdout, one line per request: method, path, status, duration. Denials log their reason and the remaining balance. |
| Metrics / traces | `X-Response-Time-Ms`, `X-Fusegrid-Version`; and per successful proxy call, `x-fusegrid-reserved-usd`, `x-fusegrid-actual-usd`, `x-fusegrid-released-usd` — so a caller can audit settlement from the response alone. |
| Configuration | None required. No environment variables, no secrets, no database. Prices and ceilings are literals in `api/index.py`. |
| Rate limiting | 120 requests / 60 s per client host, in-memory sliding window. **Per-instance and resets on cold start** — stated rather than described as rate limiting, because on serverless that is what it is. `/demo/overrun` caps concurrency at 60 so the demo cannot be used to load the deployment. |
| Failure / degradation mode | **Fail closed.** A ledger that cannot be reached returns `503` rather than admitting unenforced spend. Unhandled exceptions collapse to one opaque `500`, since an error that varies with ledger internals discloses more than it helps. An unpriced model is refused, never charged at zero. |
| Store | `MemoryStore`, so state is per-instance and lost on cold start. Correct for a demo and **wrong for production behind a load balancer** — which is measured failure #1 in `bench/baseline/`. `RedisStore` is the deployable one; using it here would mean provisioning Redis to demonstrate a property `MemoryStore` already demonstrates. |

## The reset endpoint

A shared instance with small ceilings is exhausted by the first visitor, and a demo nobody after
the first can run is not a demo. `/demo/reset` clears recorded spend.

It is the only mutating endpoint, it can only ever *reduce* recorded spend, and a test asserts it
leaves every ceiling unchanged — so it cannot be used to raise a ceiling or bypass one.

It also clears **in place** rather than rebinding the ledger. Rebinding looked correct and was
not: `create_app` captures the ledger in a closure, so after a reset the proxy kept writing to
the orphaned ledger while `/v1/budgets` read the new one — spend appeared to vanish while the
real ceiling silently kept counting.

## Deploy

```bash
uv build --wheel        # catches packaging errors the platform would hit
pytest -q               # 59 tests, incl. 19 against the deployed app
vercel --prod
```

## Rollback

```bash
vercel rollback                                  # previous production deployment
vercel ls fusegrid && vercel promote <url>       # or a specific one
```

Stateless with no database and no migrations. The in-memory ledger is discarded either way.
