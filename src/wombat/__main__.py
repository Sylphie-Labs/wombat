"""``python -m wombat`` — boot the ONE standing wombat process (TK-53).

TK-259 (DEC-52a/DEC-53b): log custody for a wombat boot lives HERE, in the entry point, and
ONLY here — never in a launcher script (the wombat-console.ps1 Tee-Object proved unreliable:
block-buffered piped stderr plus Tee-Object creating its file only on first-object-received
meant healthy boots could produce zero bytes on disk). ``main()`` configures the root logger
exactly once at boot: keep the existing stderr behavior AND add a ``FileHandler`` writing
``logs/runtime-<yyyyMMdd-HHmmss>.log`` under the cwd (created if absent), flushing per emit
(the stdlib default), then emits a boot banner as the first record. Importing ``wombat`` as a
library, or running the ``wombat.settings_app`` child process, adds ZERO handlers — this
function is the only place handlers are attached.

Immediately after handler setup, stdlib ``faulthandler`` is enabled targeting the SAME per-boot
log file's open stream (DEC-53b, binding amendment) — the ISS-15 live diagnosis proved an
exit-139 native fault (access violation / segfault) leaves plain Python logging with nothing to
say; faulthandler dumps thread stacks straight into the runtime-owned file instead. The file
stream is kept open for the process lifetime so a native fault can still write into it.

LAST-GASP: ``asyncio.run(serve())`` is wrapped so any terminating exception is logged CRITICAL
with its full traceback plus an explicit, honest shutdown line before the process exits nonzero
— a death is never traceless (short of a SIGKILL-class kill, whose signature is a boot-bannered
file with no shutdown line — the accepted honesty bar, DEC-52a). A cooperative
``KeyboardInterrupt``/``asyncio.CancelledError`` logs only the honest shutdown line, no traceback
spew, since that is a clean-shutdown path, not a fatal one.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import sys
from datetime import datetime
from pathlib import Path

from wombat.runtime import serve

_LOG_DIR_NAME = "logs"
_LOGGER = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Configure the root logger exactly once, for this boot only (TK-259). Adds a stderr
    handler (preserving the prior stderr-only behavior) plus a per-boot ``FileHandler`` under
    ``logs/`` relative to the cwd, then emits the first record — a boot banner — and enables
    ``faulthandler`` targeting the same open file stream (DEC-53b)."""
    log_dir = Path(_LOG_DIR_NAME)
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"runtime-{timestamp}.log"

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    # ``logging.FileHandler`` opens (and owns) the file's stream itself; that stream is
    # deliberately kept open for process lifetime (never closed here) so faulthandler below can
    # keep writing into it for the rest of the boot, including past a native fault.
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)
    root.addHandler(file_handler)

    _LOGGER.info("wombat runtime boot — logging to %s", log_path)

    # DEC-53b (binding TK-259 amendment): faulthandler targets the SAME per-boot log file
    # stream so a native-level fault leaves thread stacks in the runtime-owned log. FileHandler
    # opens its stream eagerly (delay=False, the default) so it is never None here.
    assert file_handler.stream is not None
    faulthandler.enable(file=file_handler.stream)


def main() -> None:
    """Console-script entry point (TK-237) — same boot as ``python -m wombat``.

    TK-259: configures runtime-owned per-boot file logging + faulthandler exactly once, then
    runs ``serve()`` under a last-gasp wrapper so a terminating exception is never traceless.
    """
    _configure_logging()
    try:
        asyncio.run(serve())
    except (KeyboardInterrupt, asyncio.CancelledError):
        _LOGGER.info("wombat runtime shutting down (interrupted) — clean shutdown.")
    except BaseException:
        _LOGGER.critical("wombat runtime terminating on unhandled exception", exc_info=True)
        _LOGGER.critical("wombat runtime shutting down (fatal) — process exiting nonzero.")
        sys.exit(1)
    else:
        _LOGGER.info("wombat runtime shutting down (serve returned) — clean shutdown.")


if __name__ == "__main__":
    main()
