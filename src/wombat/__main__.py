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

ARGV DISPATCH (TK-335, DEC-77 r1): ``main()``'s FIRST act, before ANY of the above, is parsing
``sys.argv``. Bare ``python -m wombat`` (empty argv) is byte-identical to every boot before this
ticket. ``wipe`` runs ``wombat.wipe``'s archive-then-wipe engine instead — it NEVER configures
per-boot logging, NEVER writes ``logs/runtime-*.log``, NEVER binds the singleton port, and NEVER
calls ``serve()``. It is dry-run by default (prints the enumeration it would archive/wipe plus
the archive path, touches nothing, exits nonzero); ``--confirm`` performs it, after refusing
against a live runtime (DEC-77 r2 — probing BOTH the chat handshake port and the singleton port
via ``wombat.runtime``'s existing TK-268 helpers, never a third probe implementation) and the
durable-substrate guard (DEC-77 r7, ``wombat.wipe.check_substrate_guard``). An unrecognized
subcommand or flag is argparse's own usage-to-stderr, exit-2 behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import logging
import socket
import sys
from datetime import datetime
from pathlib import Path

from wombat.config import ConfigurationError, WombatConfig, load_config
from wombat.runtime import _existing_handshake_port, _handshake_port_is_live, serve
from wombat.trail.renderer import _DEFAULT_LOG_PATH
from wombat.wipe import (
    WipeAborted,
    archive_and_wipe,
    check_substrate_guard,
    wipe_filesystem_tier,
)

_LOG_DIR_NAME = "logs"
_ARCHIVE_DIR_NAME = "archives"
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


def _build_arg_parser() -> argparse.ArgumentParser:
    """DEC-77 r1: the whole argv surface is one optional ``wipe`` subcommand. An unrecognized
    subcommand or flag is argparse's own usage-to-stderr, exit-2 behavior — never a hand-rolled
    third implementation of that. Empty argv (``sys.argv[1:] == []``) parses to
    ``Namespace(command=None)`` with zero output — the bare-boot path stays untouched."""
    parser = argparse.ArgumentParser(prog="wombat")
    subparsers = parser.add_subparsers(dest="command")
    wipe_parser = subparsers.add_parser(
        "wipe", help="archive-then-wipe wombat's persisted memory (dry-run by default)"
    )
    wipe_parser.add_argument(
        "--confirm", action="store_true", help="perform the wipe (default: dry-run)"
    )
    wipe_parser.add_argument(
        "--archive-dir",
        type=str,
        default=None,
        help="override the archive root (default: archives/wipe-<yyyyMMdd-HHmmss>/)",
    )
    return parser


def _default_wipe_archive_dir() -> Path:
    """``archives/wipe-<yyyyMMdd-HHmmss>/`` relative to cwd (DEC-77 r3, mirroring TK-259's
    ``logs/`` convention) — no new config field, no new env var."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(_ARCHIVE_DIR_NAME) / f"wipe-{timestamp}"


def _is_runtime_live(config: WombatConfig) -> bool:
    """DEC-77 r2: refuse iff EITHER the chat handshake file's recorded port OR the singleton
    port answers on loopback — reusing ``wombat.runtime``'s TK-268 helpers (never a third probe
    implementation). Handshake-only would silently permit a split-brain wipe on a chat-disabled
    boot: the singleton bind is universal to every runtime, while the handshake file is written
    only when ``wombat_chat_handshake_file`` is set. Fail-closed: either signal is enough."""
    if config.wombat_chat_handshake_file:
        port = _existing_handshake_port(Path(config.wombat_chat_handshake_file))
        if port is not None and _handshake_port_is_live(port):
            return True
    return _handshake_port_is_live(config.wombat_singleton_port)


def _wipe_plan_lines(config: WombatConfig, archive_dir: Path) -> list[str]:
    """The full enumeration ``wipe`` WOULD archive and wipe (AC4) — config-driven only, so this
    never touches Postgres or the filesystem and is always safe to print, live runtime or not."""
    return [
        "wombat wipe would archive then wipe:",
        f"  - Postgres ({config.wombat_pg_dsn or '<WOMBAT_PG_DSN unset>'}): every public base "
        "table except wombat_settings (archived, then TRUNCATEd)",
        f"  - brief: {config.wombat_brief_path or '<unset>'}",
        f"  - feedback: {config.wombat_feedback_file or '<unset>'}",
        f"  - trail log: {_DEFAULT_LOG_PATH} (+ its .sidecar.json cursor, deleted)",
        f"  - ASR voice drop: {config.wombat_asr_drop_dir or '<unset>'} "
        "(incl. processed/ and failed/)",
        f"  - archive directory: {archive_dir}",
    ]


def _run_wipe_command(*, confirm: bool, archive_dir_override: str | None) -> int:
    """``python -m wombat wipe`` (TK-335). Dry-run by default (AC4): prints the enumeration and
    archive path, touches nothing, returns nonzero. ``--confirm`` (AC5) refuses against a live
    runtime (AC6, DEC-77 r2) and a configured durable substrate (AC3, DEC-77 r7) BEFORE any
    archive or destructive act, then performs the Postgres tier (TK-334) followed by the
    filesystem tier (TK-335), printing the archive directory as the final stdout line on success.

    Batch-review repair (round 3, minor finding): ``load_config()`` can raise
    ``ConfigurationError`` (e.g. a missing required env var) — caught here and printed as the
    SAME clean ``wombat wipe: aborted - <reason>`` line every other failure mode in this command
    already produces, rather than a raw Python traceback. Exit code stays nonzero either way (the
    ps1/Electron exit-code contract is unchanged).
    """
    try:
        config = load_config()
    except ConfigurationError as exc:
        print(f"wombat wipe: aborted - {exc}", file=sys.stderr)
        return 1
    archive_dir = (
        Path(archive_dir_override) if archive_dir_override else _default_wipe_archive_dir()
    )

    if not confirm:
        for line in _wipe_plan_lines(config, archive_dir):
            print(line)
        return 1

    if not config.wombat_pg_dsn:
        print(
            "wombat wipe: aborted - WOMBAT_PG_DSN is not set; nothing to wipe.", file=sys.stderr
        )
        return 1

    if _is_runtime_live(config):
        print(
            "wombat wipe: refusing - a wombat runtime is LIVE (the chat handshake port or the "
            "singleton port answered on loopback). Stop the runtime first, then re-run "
            "`python -m wombat wipe --confirm`.",
            file=sys.stderr,
        )
        return 1

    trail_log_path = Path(_DEFAULT_LOG_PATH)
    try:
        substrate = check_substrate_guard()
        pg_report = archive_and_wipe(config.wombat_pg_dsn, archive_dir, substrate=substrate)
        wipe_filesystem_tier(
            archive_dir,
            brief_path=Path(config.wombat_brief_path) if config.wombat_brief_path else None,
            feedback_path=(
                Path(config.wombat_feedback_file) if config.wombat_feedback_file else None
            ),
            trail_log_path=trail_log_path,
            asr_drop_dir=(
                Path(config.wombat_asr_drop_dir) if config.wombat_asr_drop_dir else None
            ),
        )
    except WipeAborted as exc:
        print(f"wombat wipe: aborted - {exc}", file=sys.stderr)
        return 1

    print(str(pg_report.archive_dir))
    return 0


def main() -> None:
    """Console-script entry point (TK-237) — same boot as ``python -m wombat``.

    TK-335 (DEC-77 r1): argv dispatch is the FIRST act, before any of the below. ``wipe`` runs
    ``_run_wipe_command`` and returns without ever configuring logging, binding the singleton
    port, or calling ``serve()``. Empty argv falls through to the unchanged legacy boot.

    TK-259: configures runtime-owned per-boot file logging + faulthandler exactly once, then
    runs ``serve()`` under a last-gasp wrapper so a terminating exception is never traceless.

    TK-261 (DEC-52e): before ``serve()`` is invoked, binds the singleton loopback port as a
    single-instance guard. A bind failure logs exactly one loud ERROR line naming the port and
    the config field into this boot's own per-boot log, then exits nonzero fast — no traceback
    spew, since a concurrent-instance refusal is an expected launch outcome, not a fatal one.
    """
    args = _build_arg_parser().parse_args(sys.argv[1:])
    if args.command == "wipe":
        sys.exit(_run_wipe_command(confirm=args.confirm, archive_dir_override=args.archive_dir))

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
