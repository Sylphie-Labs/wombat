"""wombat.cost — mouth spend accounting (TK-9, EP-5): the daily token-spend ledger.

Layer 2 of TK-9's two-layer mouth budget. Layer 1 (cog-worx's per-drive ``BudgetPolicy``/
``BudgetGuard``) lives in the installed ``cogworx`` package and is wired from
``wombat.bootstrap``; this package owns only the wombat-side durable daily cumulative-token
ceiling (``daily_spend_ledger.py``), a thin wrapper over the shared ``wombat.domain.daily_ledger``
primitive (TK-152).
"""
