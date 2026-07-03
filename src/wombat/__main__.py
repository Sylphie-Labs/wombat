"""``python -m wombat`` — boot the ONE standing wombat process (TK-53)."""

from __future__ import annotations

import asyncio

from wombat.runtime import serve

if __name__ == "__main__":
    asyncio.run(serve())
