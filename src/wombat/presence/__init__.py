"""wombat.presence — THROWAWAY de-risk spike (TK-4, RISK-3).

Prototype probe for laptop user-state. Reads ONE OS idle signal (Windows
``GetLastInputInfo`` via ctypes) and classifies active/idle, flagging stale
snapshots so the gate defaults to HOLD. Not the production SourceRegistry
(TK-3) and not the hardened presence_hold (TK-11) — those come later.
"""
