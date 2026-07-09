"""wombat.safety.local_residency — the structural same-host storage boundary (TK-150, EP-28).

NG-7/CON-7/ASMP-1 made structural: every persistence write must target THIS host; the DeepSeek
phrasing call is the ONE allowed egress (ASMP-1). This module supplies the ONE residency
predicate consumed at the seams this ticket un-holds:

  - ``residency_check`` is assignable to ``wombat.substrate.ResidencyCheck`` — TK-14's
    ``real_adapter_bundle`` runs it on every endpoint (``pg_dsn``, ``neo4j_uri``) BEFORE
    constructing any adapter (TK-54 -> TK-47's declared consumer seam).
  - ``check_config`` is the startup guard ``wombat.runtime.serve()`` calls right after
    ``load_config()`` — it applies ``residency_check`` to ``config.wombat_pg_dsn`` when set.
    ``config.deepseek_base_url`` is DELIBERATELY EXEMPT: it is the ONE allowed egress
    (ASMP-1) and is never residency-checked.

Q-25 residency rule: same-HOST, not the literal ``"localhost"`` alone — loopback addresses
(``127.0.0.0/8``, ``::1``), the literal ``"localhost"``, Unix-domain socket paths, and any
address that RESOLVES to one of this host's own interface addresses (covers a Docker-bridge/
service name resolving to self) all PASS. An address resolving to a genuinely different host
FAILS, raising ``RemoteStorageConfigError`` naming the offending endpoint.

TK-178 (CR2-1): ``hostaddr=`` (URL query key, keyword token, or bare with no ``host=`` at all) is
the address libpq/psycopg actually dial — ``host=``/``?host=`` is then only auth/TLS SNI. When
both are present the residency check runs against ``hostaddr``; see ``_extract_host``.

Q-87 ruling 3: the resolution seam is INJECTED (``resolver``: hostname -> list of IPs;
``local_addrs``: this host's own interface addresses) with stdlib-``socket`` defaults, via
``make_residency_check`` — this keeps the table-driven test battery deterministic and
Windows-safe (no real DNS/interface enumeration required in a test). ``residency_check`` (the
module-level export every real seam consumes) is ``make_residency_check()`` bound to those
stdlib defaults.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

from wombat.config import WombatConfig


class RemoteStorageConfigError(RuntimeError):
    """Raised when a persistence endpoint does not resolve to THIS host (NG-7/CON-7/ASMP-1)."""


# hostname -> the IP addresses it resolves to (Q-87 ruling 3 — the injected resolution seam).
Resolver = Callable[[str], list[str]]
# This host's own interface addresses (Q-87 ruling 3 — the injected local-addrs seam).
LocalAddrsProvider = Callable[[], list[str]]

# Matches substrate.ResidencyCheck (Callable[[str], None]) BY SIGNATURE (AC5) — never imported
# from substrate.py directly (that would be a circular import; wombat.substrate imports nothing
# from this module, it only declares the alias TK-150 must satisfy).
ResidencyCheck = Callable[[str], None]


def _default_resolver(hostname: str) -> list[str]:
    """The stdlib-``socket`` default resolver: every A/AAAA address ``hostname`` resolves to."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return []
    return sorted({str(info[4][0]) for info in infos})


def _default_local_addrs() -> list[str]:
    """The stdlib-``socket`` default local-addrs provider: this host's own interface addresses."""
    addrs: set[str] = {"127.0.0.1", "::1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addrs.add(str(info[4][0]))
    except OSError:
        pass
    return sorted(addrs)


def _strip_brackets(host: str) -> str:
    """Strip the ``[...]`` IPv6-literal brackets URI hosts carry, if present."""
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _extract_host(endpoint: str) -> str | None:
    """Return the address libpq actually dials for a DSN/URI, or ``None`` when the endpoint
    denotes a local path (a bare filesystem path or a Unix-domain socket — which can never be
    off-host) or omits a host entirely (Postgres' own convention: no host means the local Unix
    socket).

    TK-178 (CR2-1): libpq/psycopg dial ``hostaddr=`` when present — ``host=``/``?host=`` then
    only supplies auth/TLS SNI, it is NEVER the TCP connection target. So whenever ``hostaddr``
    is present (URL query key OR keyword token — including the bare-hostaddr-no-host form) it
    takes precedence over ``host`` here, matching what libpq actually connects to. ``hostaddr``
    absent -> behavior is byte-identical to the pre-TK-178 host-only extraction.
    """
    stripped = endpoint.strip()
    if stripped.startswith("/"):
        return None  # a bare filesystem/unix-socket path

    if "://" in stripped:
        parsed = urlsplit(stripped)
        query = parse_qs(parsed.query)
        query_hostaddr = query.get("hostaddr", [None])[0]
        if query_hostaddr:
            return query_hostaddr  # the actual TCP dial target — wins over host/?host=
        if parsed.hostname:
            return parsed.hostname
        query_host = query.get("host", [None])[0]
        if not query_host or query_host.startswith("/"):
            return None  # no host, or an explicit unix-socket path via ?host=/...
        return query_host

    # A keyword=value DSN (e.g. "host=/var/run/postgresql dbname=wombat user=wombat").
    keyword_hostaddr: str | None = None
    keyword_host: str | None = None
    for token in stripped.split():
        if token.startswith("hostaddr="):
            keyword_hostaddr = token[len("hostaddr=") :]
        elif token.startswith("host="):
            keyword_host = token[len("host=") :]
    if keyword_hostaddr:
        return keyword_hostaddr  # the actual TCP dial target — wins over host, even bare
    if keyword_host is not None:
        return None if (not keyword_host or keyword_host.startswith("/")) else keyword_host
    return None  # no "host="/"hostaddr=" keyword present -> the local Unix socket default


def _addr_is_local(addr: str, local_addrs: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(_strip_brackets(addr))
    except ValueError:
        return False
    return ip.is_loopback or str(ip) in local_addrs


def make_residency_check(
    *,
    resolver: Resolver = _default_resolver,
    local_addrs: LocalAddrsProvider = _default_local_addrs,
) -> ResidencyCheck:
    """Build a ``ResidencyCheck`` over an injected ``resolver``/``local_addrs`` (Q-87 ruling 3).

    ``residency_check`` (module level) is this factory bound to the stdlib-``socket`` defaults —
    every real call site uses it. Tests call this factory directly with fake, deterministic
    resolver/local-addrs functions for the table-driven battery.
    """

    def _check(endpoint: str) -> None:
        host = _extract_host(endpoint)
        if host is None:
            return  # a unix-socket / no-host form — inherently local, never off-host
        stripped_host = _strip_brackets(host)

        if stripped_host.lower() == "localhost":
            return

        try:
            literal_ip = ipaddress.ip_address(stripped_host)
        except ValueError:
            literal_ip = None

        locals_now = local_addrs()
        if literal_ip is not None:
            if literal_ip.is_loopback or str(literal_ip) in locals_now:
                return
        else:
            resolved = resolver(stripped_host)
            if any(_addr_is_local(addr, locals_now) for addr in resolved):
                return

        raise RemoteStorageConfigError(
            f"storage endpoint does not resolve to this host: {endpoint!r} (host={host!r})"
        )

    return _check


# The ONE production predicate every real seam (substrate.ResidencyCheck, check_config below)
# consumes — bound to the stdlib-socket defaults.
residency_check: ResidencyCheck = make_residency_check()


def check_config(config: WombatConfig) -> None:
    """Apply ``residency_check`` to every persistence endpoint on ``config``.

    Currently just ``config.wombat_pg_dsn`` (the only persistence DSN ``WombatConfig`` carries
    today — TK-13's ``SubstrateConfig``/``neo4j_uri`` are not sourced from ``WombatConfig``, see
    ``wombat.substrate.build_substrate``'s own ``residency_check`` seam for that path).
    ``config.deepseek_base_url`` is DELIBERATELY EXEMPT — the ONE ASMP-1 egress — and is never
    passed to ``residency_check`` here. A refusal names the offending config key.
    """
    if config.wombat_pg_dsn:
        try:
            residency_check(config.wombat_pg_dsn)
        except RemoteStorageConfigError as exc:
            raise RemoteStorageConfigError(f"wombat_pg_dsn: {exc}") from exc


__all__ = [
    "LocalAddrsProvider",
    "RemoteStorageConfigError",
    "ResidencyCheck",
    "Resolver",
    "check_config",
    "make_residency_check",
    "residency_check",
]
