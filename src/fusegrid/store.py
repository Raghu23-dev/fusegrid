"""Store implementations.

`MemoryStore` is the reference: a threading.Lock makes reserve genuinely atomic, which
is enough for a single process and is what the test suite runs against.

`RedisStore` is the deployable one. Atomicity comes from a Lua script, so the
read-modify-write happens inside Redis in one indivisible step — the property that
closes the check-then-act race measured in bench/baseline/failopen.py.
"""

from __future__ import annotations

import threading
from typing import Any

from .ledger import LedgerUnavailable

# Money is stored as integer micro-dollars, never as float.
#
# WHY: the adversarial suite caught this. With float accumulation, twenty $0.05
# reservations sum to 1.0000000000000002, which exceeds a $1.00 ceiling by 2.2e-16 —
# so the twentieth legitimate request was DENIED. A budget that rejects a request it
# should allow is as broken as one that admits a request it should not, and this class
# of error is exactly why currency is never floating point.
#
# One micro-dollar resolution is far finer than any provider's per-token price and
# leaves headroom for a ledger in the billions.
MICROS = 1_000_000


def to_micros(usd: float) -> int:
    """Convert dollars to integer micro-dollars, rounding half-up.

    Rounding up on a reservation would systematically over-reserve; rounding down would
    systematically under-charge. Half-up is unbiased, and at micro-dollar resolution the
    residual is far below any real price.
    """
    return int(usd * MICROS + (0.5 if usd >= 0 else -0.5))


def to_usd(micros: int) -> float:
    return micros / MICROS


class MemoryStore:
    """Single-process store. Atomic via a lock, integer arithmetic internally.

    Correct for one replica and useless for two — which is precisely measured failure
    #1, so the docstring says so rather than letting someone deploy it behind a load
    balancer and discover it.
    """

    def __init__(self) -> None:
        self._held: dict[str, int] = {}  # micro-dollars
        self._lock = threading.Lock()
        # Test hook: make every operation raise, to verify fail-closed behaviour.
        self.fail = False

    def reserve(self, key: str, amount: float, ceiling: float) -> tuple[bool, float]:
        if self.fail:
            raise LedgerUnavailable("memory store: injected failure")
        amt, cap = to_micros(amount), to_micros(ceiling)
        with self._lock:
            current = self._held.get(key, 0)
            if current + amt > cap:
                return False, to_usd(max(0, cap - current))
            self._held[key] = current + amt
            return True, to_usd(cap - self._held[key])

    def settle(self, key: str, reserved: float, actual: float) -> None:
        if self.fail:
            raise LedgerUnavailable("memory store: injected failure")
        delta = to_micros(reserved) - to_micros(actual)
        with self._lock:
            self._held[key] = max(0, self._held.get(key, 0) - delta)

    def release(self, key: str, amount: float) -> None:
        if self.fail:
            raise LedgerUnavailable("memory store: injected failure")
        amt = to_micros(amount)
        with self._lock:
            self._held[key] = max(0, self._held.get(key, 0) - amt)

    def spent(self, key: str) -> float:
        with self._lock:
            return to_usd(self._held.get(key, 0))

    def clear(self) -> None:
        """Drop all recorded spend. Ceilings live in the Ledger and are untouched.

        For tests and for resettable demos. Takes the same lock as `reserve`, because a clear
        racing a reservation could otherwise leave a reservation outstanding against a store
        that has forgotten it.
        """
        with self._lock:
            self._held.clear()


# Reserve, atomically, inside Redis. Returned as a pair so the caller learns the
# remaining balance in the same round trip rather than issuing a second read that
# would already be stale.
# Integer micro-dollars, so INCRBY rather than INCRBYFLOAT. Redis INCRBYFLOAT carries
# the same accumulation error that denied a legitimate request in the memory store.
RESERVE_LUA = """
local key     = KEYS[1]
local amount  = tonumber(ARGV[1])
local ceiling = tonumber(ARGV[2])
local current = tonumber(redis.call('GET', key) or '0')
if current + amount > ceiling then
  local remaining = ceiling - current
  if remaining < 0 then remaining = 0 end
  return {0, remaining}
end
local after = redis.call('INCRBY', key, amount)
return {1, ceiling - after}
"""


class RedisStore:
    """Deployable store. Atomicity via a Lua script (one round trip)."""

    def __init__(self, client: Any, prefix: str = "fusegrid:spend:") -> None:
        self._redis = client
        self._prefix = prefix
        try:
            self._reserve = client.register_script(RESERVE_LUA)
        except Exception as exc:
            raise LedgerUnavailable(f"could not register reserve script: {exc}") from exc

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def reserve(self, key: str, amount: float, ceiling: float) -> tuple[bool, float]:
        try:
            allowed, remaining = self._reserve(
                keys=[self._k(key)], args=[to_micros(amount), to_micros(ceiling)]
            )
        except Exception as exc:
            raise LedgerUnavailable(f"reserve failed: {exc}") from exc
        return bool(int(allowed)), to_usd(int(remaining))

    def settle(self, key: str, reserved: float, actual: float) -> None:
        delta = to_micros(reserved) - to_micros(actual)
        if delta == 0:
            return
        try:
            self._redis.incrby(self._k(key), -delta)
        except Exception as exc:
            # A failed settle over-charges the budget rather than under-charging it.
            # That is the safe direction: the ceiling still holds, the key just has
            # less headroom until the window resets. Raising makes it visible.
            raise LedgerUnavailable(f"settle failed: {exc}") from exc

    def release(self, key: str, amount: float) -> None:
        try:
            self._redis.incrby(self._k(key), -to_micros(amount))
        except Exception as exc:
            raise LedgerUnavailable(f"release failed: {exc}") from exc

    def spent(self, key: str) -> float:
        try:
            return to_usd(int(self._redis.get(self._k(key)) or 0))
        except Exception as exc:
            raise LedgerUnavailable(f"read failed: {exc}") from exc
