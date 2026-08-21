"""Model pricing: maximum cost per request.

An unknown model has NO price, never a zero price. That distinction is the whole
module: measured failure #4 in bench/baseline/failopen.py is a pricing table treating
an absent model as free, which silently removes enforcement for exactly the newest and
most expensive models.
"""

from __future__ import annotations

from dataclasses import dataclass


class UnpricedModel(LookupError):
    """No price is configured for this model, so no maximum can be reserved."""


class InvalidTokenCount(ValueError):
    """A token count was negative, so it cannot be priced.

    A negative count is not a small cost — it is a *credit*, and a request priced
    below zero reduces the reservation instead of consuming it. Raising here rather
    than clamping makes the caller's bad input visible: silently treating -5 as 0
    would admit a request whose real output length is unknown.
    """


def _reject_negative(**counts: int) -> None:
    """Refuse negative token counts.

    Both pricing paths were unguarded, and a caller sending `max_tokens: -5` was served
    for exactly $0.000000 against an exhausted ceiling — unbounded free requests where a
    well-formed one got 429. `int(-5 * 0.8) = -4` output tokens made the settled cost a
    credit, so spend never moved. Found by end-user testing the deployed instance, not by
    the suite: every existing test passed a plausible positive value.
    """
    for name, value in counts.items():
        if value < 0:
            raise InvalidTokenCount(
                f"{name} must be >= 0, got {value}. A negative token count would price "
                "the request as a credit and let it bypass the ceiling."
            )


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Per-million-token prices, as providers publish them."""

    input_per_mtok: float
    output_per_mtok: float

    def max_cost(self, input_tokens: int, max_output_tokens: int) -> float:
        """Worst-case cost of a request that has not happened yet.

        Uses max_output_tokens rather than an estimate, because the true output length
        is unknowable in advance and over-reserving is recoverable while
        under-reserving is not.
        """
        _reject_negative(input_tokens=input_tokens, max_output_tokens=max_output_tokens)
        return (
            input_tokens / 1_000_000 * self.input_per_mtok
            + max_output_tokens / 1_000_000 * self.output_per_mtok
        )

    def actual_cost(self, input_tokens: int, output_tokens: int) -> float:
        _reject_negative(input_tokens=input_tokens, output_tokens=output_tokens)
        return (
            input_tokens / 1_000_000 * self.input_per_mtok
            + output_tokens / 1_000_000 * self.output_per_mtok
        )


class Pricing:
    """Model → price. Configured, never guessed."""

    def __init__(self, prices: dict[str, ModelPrice], default_max_output: int = 4096) -> None:
        self._prices = dict(prices)
        self._default_max_output = default_max_output

    def price(self, model: str) -> ModelPrice:
        p = self._prices.get(model)
        if p is None:
            raise UnpricedModel(
                f"no price configured for model {model!r}. "
                "fusegrid denies unpriced models rather than costing them at zero — "
                "add it to the pricing config."
            )
        return p

    def max_cost(self, model: str, input_tokens: int, max_output_tokens: int | None) -> float:
        return self.price(model).max_cost(
            input_tokens,
            max_output_tokens if max_output_tokens is not None else self._default_max_output,
        )

    def actual_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        return self.price(model).actual_cost(input_tokens, output_tokens)

    def known_models(self) -> list[str]:
        return sorted(self._prices)
