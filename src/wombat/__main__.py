"""``python -m wombat`` — boot the ONE standing wombat process (TK-53)."""

from __future__ import annotations

import asyncio

from wombat.runtime import serve


def main() -> None:
    """Console-script entry point (TK-237) — same boot as ``python -m wombat``."""
    asyncio.run(serve())


if __name__ == "__main__":
    main()
