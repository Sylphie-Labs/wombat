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

SINGLETON GUARD (TK-261, DEC-52e): ISS-14 saw three wombat runtimes running live at once — the
only guard was the launcher script. After logging is configured (TK-259) but before ``serve()`` is
ever invoked, ``main()`` binds an exclusive loopback TCP socket on
``WombatConfig.wombat_singleton_port`` (default 63218). The bound socket is held for process
lifetime — never ``accept()``-ed, never listened to for traffic; it exists purely as an OS-level
mutex. It is deliberately NEVER explicitly released anywhere in this module: the OS reclaims the
port on ANY process death, including a hard kill, so no stale-lock state can ever exist and no
cleanup path is needed (or permitted). A bind failure (the port is already held by a prior live
instance) logs exactly one loud ``ERROR`` naming both the port and the ``wombat_singleton_port``
config field into that boot's own per-boot log (TK-259), then exits nonzero immediately, before
``serve()`` is ever reached.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import socket
import sys
from datetime import datetime
from pathlib import Path

from wombat.config import load_config
from wombat.runtime import serve

_LOG_DIR_NAME = "logs"
_LOGGER = logging.getLogger(__name__)

# Held for process lifetime once bound (TK-261) — never closed/released by this module; the OS
# reclaims the port on process death, including hard kills. Module-level so it outlives main()'s
# frame and is never garbage-collected (and thus never closed) while the process is alive.
_singleton_socket: socket.socket | None = None


class _SingletonLockError(Exception):
    """Raised when the singleton port bind fails — a prior wombat instance already holds it
    (TK-261). Carries the port so ``main()``'s handler can name it in the single ERROR line."""

    def __init__(self, port: int) -> None:
        super().__init__(port)
        self.port = port


def _acquire_singleton_lock(port: int) -> None:
    """Bind an exclusive loopback TCP socket on ``port`` as a single-instance guard (TK-261,
    DEC-52e). On success the bound socket is stashed in the module-level ``_singleton_socket`` and
    held for process lifetime — never accepted, never listened to; it is never explicitly closed
    by this module (the OS reclaims the port on any process death, including a hard kill, so no
    stale-lock state or cleanup path exists). On bind failure (a prior instance already holds the
    port), raises ``_SingletonLockError`` for ``main()`` to log and exit on."""
    global _singleton_socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        sock.close()
        raise _SingletonLockError(port) from exc
    _singleton_socket = sock


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

    TK-261 (DEC-52e): before ``serve()`` is invoked, binds the singleton loopback port as a
    single-instance guard. A bind failure logs exactly one loud ERROR line naming the port and
    the config field into this boot's own per-boot log, then exits nonzero fast — no traceback
    spew, since a concurrent-instance refusal is an expected launch outcome, not a fatal one.
    """
    _configure_logging()
    try:
        config = load_config()
        _acquire_singleton_lock(config.wombat_singleton_port)
        asyncio.run(serve())
    except (KeyboardInterrupt, asyncio.CancelledError):
        _LOGGER.info("wombat runtime shutting down (interrupted) — clean shutdown.")
    except _SingletonLockError as exc:
        _LOGGER.error(
            "wombat runtime already running: singleton port %d (config field "
            "wombat_singleton_port) is already bound — refusing to start a second instance.",
            exc.port,
        )
        sys.exit(1)
    except BaseException:
        _LOGGER.critical("wombat runtime terminating on unhandled exception", exc_info=True)
        _LOGGER.critical("wombat runtime shutting down (fatal) — process exiting nonzero.")
        sys.exit(1)
    else:
        _LOGGER.info("wombat runtime shutting down (serve returned) — clean shutdown.")


if __name__ == "__main__":
    main()
