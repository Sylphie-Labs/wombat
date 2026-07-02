"""wombat.presence — RETIRED (TK-4 spike, superseded by TK-11, Q-54).

The TK-4 throwaway probe (``probe.py``) has been DELETED: its logic was hardened into the
production homes ``wombat.sources.presence`` (types, OS idle reader, classify, provider) and
``wombat.gate.presence_hold`` (the pure canonical hold predicate). TK-4's record lives on in
its ``poc.result`` in the contract; this package is left empty (no re-export shim) so nothing
can import the retired module path by accident.
"""
