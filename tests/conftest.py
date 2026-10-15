"""Shared test helpers — and the guard that makes «no network» true.

A2-1 (2026-09-10): a recorded-fixture test believed it was offline and was not.
The injected mock was dropped on the second ``*_sync`` call and the request went
to the live venue — the test still passed, because a real 200 parses just as
well as a recorded one. A suite that can silently reach a money endpoint is not
verifying anything, so outbound sockets are blocked here for every test that is
not marked ``live``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(exchange: str, name: str) -> Any:
    return json.loads((FIXTURES / exchange / f"{name}.json").read_text())


# ---------------------------------------------------------------------------
# Network isolation guard (netiso-1015)
#
# Why: a test matrix run bypassed a mock and sent a fake-key auth request to the
# REAL OKX API. «Use a mock» in a brief is not a mechanism. This session-wide guard
# makes «no outbound network from tests» a property of the suite itself.
#
#   * allowed: loopback (127.0.0.0/8, ::1, localhost), unix sockets, and hosts
#     listed in env TEST_ALLOW_HOSTS (comma separated; test DB / redis only).
#   * everything else raises NetworkAccessDenied naming the host — at DNS lookup,
#     at asyncio create_connection and at socket connect/sendto.
#   * opt-out: @pytest.mark.allow_network (rejected when CI is set unless
#     ALLOW_NETWORK_TESTS=1 — only the nightly live job sets it).
#   * never allow-list a real exchange / broker host here to make a test pass —
#     fix the test (mock) instead.
# ---------------------------------------------------------------------------
import asyncio.base_events as _nig_base_events  # noqa: E402
import ipaddress as _nig_ipaddress  # noqa: E402
import os as _nig_os  # noqa: E402
import socket as _nig_socket  # noqa: E402

import pytest as _nig_pytest  # noqa: E402

_NIG_MARKERS = ("allow_network", "live")  # legacy `live` marker == allow_network
_NIG_LOOPBACK_NAMES = {"localhost", "ip6-localhost", "ip6-loopback"}
_nig_state = {"allow": False}
_nig_resolved_ok: set = set()


class NetworkAccessDenied(BaseException):
    """A test tried to reach a non-local host without @pytest.mark.allow_network.

    Derives from BaseException so product code's ``except Exception`` fallbacks
    cannot swallow it and turn a real outbound attempt into a silently green test.
    """


def _nig_allow_list() -> set:
    raw = _nig_os.environ.get("TEST_ALLOW_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _nig_host_ok(host) -> bool:
    if _nig_state["allow"] or host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    h = str(host).strip().strip("[]").lower()
    if h in ("", *_NIG_LOOPBACK_NAMES) or h in _nig_allow_list() or h in _nig_resolved_ok:
        return True
    try:
        return _nig_ipaddress.ip_address(h.split("%")[0]).is_loopback
    except ValueError:
        return False


def _nig_deny(host, via: str):
    log = _nig_os.environ.get("NETISO_LOG")  # audit aid: log every blocked attempt
    if log:
        with open(log, "a") as fh:
            fh.write(f"{host}\t{via}\t{_nig_os.environ.get('PYTEST_CURRENT_TEST', '?')}\n")
    raise NetworkAccessDenied(
        f"test tried to reach non-local host {host!r} via {via}. Mock it. "
        f"Test DB/redis hosts go in TEST_ALLOW_HOSTS; a deliberate live test needs "
        f"@pytest.mark.allow_network (rejected in CI unless ALLOW_NETWORK_TESTS=1). "
        f"Never allow-list a real exchange/broker host."
    )


def _nig_install():
    sock_cls = _nig_socket.socket
    real = {
        "connect": sock_cls.connect,
        "connect_ex": sock_cls.connect_ex,
        "sendto": sock_cls.sendto,
        "create_connection": _nig_socket.create_connection,
        "getaddrinfo": _nig_socket.getaddrinfo,
        "loop_cc": _nig_base_events.BaseEventLoop.create_connection,
    }

    def _addr_host(sock, address):
        if getattr(sock, "family", None) == getattr(_nig_socket, "AF_UNIX", object()):
            return None
        if isinstance(address, tuple) and address:
            return address[0]
        return address

    def connect(self, address):
        host = _addr_host(self, address)
        if not _nig_host_ok(host):
            _nig_deny(host, "socket.connect")
        return real["connect"](self, address)

    def connect_ex(self, address):
        host = _addr_host(self, address)
        if not _nig_host_ok(host):
            _nig_deny(host, "socket.connect_ex")
        return real["connect_ex"](self, address)

    def sendto(self, data, *args):
        host = _addr_host(self, args[-1]) if args else None
        if not _nig_host_ok(host):
            _nig_deny(host, "socket.sendto")
        return real["sendto"](self, data, *args)

    def create_connection(address, *a, **kw):
        if not _nig_host_ok(address[0]):
            _nig_deny(address[0], "socket.create_connection")
        return real["create_connection"](address, *a, **kw)

    def getaddrinfo(host, *a, **kw):
        if not _nig_host_ok(host):
            _nig_deny(host, "socket.getaddrinfo (DNS)")
        result = real["getaddrinfo"](host, *a, **kw)
        if isinstance(host, str) and host.strip().lower() in _nig_allow_list():
            _nig_resolved_ok.update(r[4][0] for r in result)  # allow-listed name -> its IPs
        return result

    async def loop_create_connection(self, protocol_factory, host=None, port=None, **kw):
        if kw.get("sock") is None and not _nig_host_ok(host):
            _nig_deny(host, "asyncio loop.create_connection")
        return await real["loop_cc"](self, protocol_factory, host, port, **kw)

    sock_cls.connect, sock_cls.connect_ex, sock_cls.sendto = connect, connect_ex, sendto
    _nig_socket.create_connection = create_connection
    _nig_socket.getaddrinfo = getaddrinfo
    _nig_base_events.BaseEventLoop.create_connection = loop_create_connection

    def restore():
        sock_cls.connect, sock_cls.connect_ex, sock_cls.sendto = real["connect"], real["connect_ex"], real["sendto"]
        _nig_socket.create_connection = real["create_connection"]
        _nig_socket.getaddrinfo = real["getaddrinfo"]
        _nig_base_events.BaseEventLoop.create_connection = real["loop_cc"]

    return restore


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_network: test deliberately uses the real network (rejected in CI unless ALLOW_NETWORK_TESTS=1)",
    )


@_nig_pytest.fixture(autouse=True, scope="session")
def _network_guard_session():
    restore = _nig_install()
    try:
        yield
    finally:
        restore()


@_nig_pytest.fixture(autouse=True)
def _network_guard_marker_gate(request):
    marked = any(request.node.get_closest_marker(m) for m in _NIG_MARKERS)
    if marked:
        in_ci = _nig_os.environ.get("CI") or _nig_os.environ.get("GITHUB_ACTIONS")
        if in_ci and _nig_os.environ.get("ALLOW_NETWORK_TESTS") != "1":
            _nig_pytest.fail(
                "allow_network/live test rejected in CI (set ALLOW_NETWORK_TESTS=1 only in the nightly live job)",
                pytrace=False,
            )
    _nig_state["allow"] = bool(marked)
    try:
        yield
    finally:
        _nig_state["allow"] = False


# ---------------------------- end network guard ----------------------------
