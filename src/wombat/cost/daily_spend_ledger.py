"""DailySpendLedger — the mouth's wombat-owned daily TOKEN-spend ceiling (TK-9, EP-5, Q-68).

Layer 2 of TK-9's two-layer mouth budget. Layer 1 is cog-worx's per-drive-segment
``BudgetPolicy``/``BudgetGuard`` (in-memory, resets per drive; ``cost/budget.py`` in the installed
``cogworx`` package) — structural but unable to enforce a cap that survives a restart or spans
multiple drives (CF-3.0-B, deferred in cog-worx). ``DailySpendLedger`` is the wombat-owned outer
cap: a THIN wrapper over the shared, Postgres-backed ``DailyLedger`` (TK-152) whose docstring
already names this ticket as a designed consumer.

No new table, no new migration: this rides the existing ``daily_ledger`` table (migration 003)
under the fixed ``ledger_name`` ``"spend:tokens"``. ``value`` is the cumulative count of
``ModelResponse.usage.prompt_tokens + completion_tokens`` recorded today (the Q-68 token source of
truth) — per-call USD ceilings remain layer 1's concern, not this ledger's.

Load-on-start, restart-durability, and the wombat-day boundary (a laptop sleeping across midnight
never double-counts or skips a day, DEC-21) all come free from the injected ``DailyLedger`` by
construction (TK-152-proven) — this wrapper adds no date/rollover logic of its own.
"""

from __future__ import annotations

from wombat.domain.daily_ledger import DailyLedger

LEDGER_NAME = "spend:tokens"


class DailySpendLedger:
    """Cumulative daily token spend for the mouth, riding the shared ``DailyLedger`` (TK-152)."""

    def __init__(self, ledger: DailyLedger) -> None:
        self._ledger = ledger

    def tokens_spent_today(self) -> int:
        """Today's cumulative token spend (creates today's row at 0 if it doesn't exist yet)."""
        return self._ledger.current_row(LEDGER_NAME).value

    def add_tokens(self, amount: int) -> int:
        """Atomically add ``amount`` tokens to today's running total; returns the new total."""
        return self._ledger.increment(LEDGER_NAME, amount=amount).value


__all__ = ["LEDGER_NAME", "DailySpendLedger"]
