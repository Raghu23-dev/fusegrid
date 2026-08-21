# Changelog

Generated from Conventional Commits. Notable changes per release.

## [Unreleased]

### Fixed
- **Security: a negative `max_tokens` bypassed the spend ceiling.** `ModelPrice.max_cost` and
  `.actual_cost` did not clamp at zero, so `int(-5 * 0.8) = -4` output tokens priced a request as
  a credit: 30 consecutive completions were served past an exhausted budget for $0.000000 each,
  where a well-formed request at the same budget was refused with 429. Negative counts now raise
  `InvalidTokenCount`, the proxy rejects a negative `max_tokens` with 400 before pricing, and a
  usage block reporting negative tokens settles at the full reservation rather than refunding.
  `max_tokens: 0` remains valid. Found by end-user testing the deployment; the 45-test
  adversarial suite passed throughout because every case used a positive value.
- Landing page stated that refusal returns `402` with a `Retry-After` header. It returns `429`
  with neither, which `docs/02-thesis.md` had specified correctly all along. A test now asserts
  the page cannot advertise a status the proxy does not return.
