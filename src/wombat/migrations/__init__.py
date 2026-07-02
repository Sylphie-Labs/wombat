"""Packaged, numbered, idempotent .sql migrations (NG-3 — no migration framework).

Each module that owns a Postgres table ships its own numbered ``NNN_*.sql`` file here and an
``ensure_schema(conn)`` function that reads + executes it via ``importlib.resources`` (see
``wombat.queue.ensure_schema`` for TK-2's ``wombat_queue`` table). Cross-file ordering is a
non-problem while each module owns one independent table (Q-46).
"""

from __future__ import annotations
