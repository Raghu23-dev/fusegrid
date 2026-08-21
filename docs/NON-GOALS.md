# Non-Goals

Deliberate scope cuts. Each was considered and rejected on a reason.

| Not doing | Why | Would reconsider if |
|---|---|---|
| **A full LLM gateway** (routing, fallbacks, caching, load balancing) | Those exist and are good. This does one thing the ecosystem does not do at all. A gateway that also enforces would be competing on the wrong axis. | Never — it would dilute the thesis. |
| **Provider-specific SDKs** | The sidecar speaks OpenAI-compatible HTTP. Every major provider and gateway offers that surface, so one shape covers the ecosystem. | A dominant provider drops OpenAI compatibility. |
| **Its own pricing database** | Pricing tables go stale, and a stale table is what causes failure #4. Prices are supplied as config, and an unknown model is *denied* rather than guessed. | Never. Guessing a price is the bug. |
| **Dashboards and reporting UI** | The value is the block, not the chart. Metrics are exposed for a real observability stack instead. | Enforcement is proven and users ask for it. |
| **Distributed consensus for the ledger** | Single-node atomic operations cover the target case. Raft for a spend counter is infrastructure theatre. | Someone needs multi-region enforcement with a measured requirement. |
| **Predicting cost from prompt content** | Token-count estimation before a call is a research problem. Reserving the configured maximum is exact and boring, and boring is correct for money. | A cheap, accurate estimator exists with published error bounds. |
| **Blocking on output-token overrun mid-stream** | Would require terminating a stream partway, leaving the caller with a truncated response and still billed. Reserving the max upfront avoids the situation entirely. | — |
| **Multi-tenant auth / RBAC** | Keys are the tenancy boundary. Identity belongs in the gateway in front. | — |
