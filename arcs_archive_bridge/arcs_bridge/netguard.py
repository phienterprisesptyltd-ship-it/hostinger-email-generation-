"""Outbound network guard.

The archive is *sealed* by default: the process may not open an outbound
socket.  This is enforced at the socket layer rather than by convention, so a
mistake in an adapter, a dependency, or a future contributor's code surfaces as
a loud :class:`NetworkBlocked` error instead of a silent upload.

The seal is lifted only by :func:`allow_network`, which every caller must reach
through an explicit user action (a CLI flag on an export command).  Lifting it
is recorded in the archive's operation log by the caller.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager

from .errors import NetworkBlocked

#: Loopback only.  Never add an empty string here: str.startswith("") is True
#: for every host, which would silently disable the seal.
_LOCAL_PREFIXES = ("127.", "::1", "localhost", "0.0.0.0")
_lock = threading.Lock()
_state = {"sealed": False, "installed": False, "reason": None}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection


def _host_of(address) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


def _is_local(address) -> bool:
    host = _host_of(address)
    if not host:
        return False
    return host.startswith(_LOCAL_PREFIXES)


def _guarded_connect(self, address):
    if _state["sealed"] and not _is_local(address):
        raise NetworkBlocked(
            f"outbound connection to {_host_of(address)!r} blocked: this archive "
            "is sealed. Archive contents are never uploaded unless you ask for it "
            "explicitly (see `arcs export --help`)."
        )
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if _state["sealed"] and not _is_local(address):
        raise NetworkBlocked(
            f"outbound connection to {_host_of(address)!r} blocked: archive sealed."
        )
    return _real_connect_ex(self, address)


def _guarded_create_connection(address, *args, **kwargs):
    if _state["sealed"] and not _is_local(address):
        raise NetworkBlocked(
            f"outbound connection to {_host_of(address)!r} blocked: archive sealed."
        )
    return _real_create_connection(address, *args, **kwargs)


def seal(reason: str = "default policy") -> None:
    """Block outbound sockets for this process."""
    with _lock:
        if not _state["installed"]:
            socket.socket.connect = _guarded_connect
            socket.socket.connect_ex = _guarded_connect_ex
            socket.create_connection = _guarded_create_connection
            _state["installed"] = True
        _state["sealed"] = True
        _state["reason"] = reason


def unseal(reason: str) -> None:
    """Lift the seal.  Only ever called from an explicit user-facing action."""
    if not reason:
        raise ValueError("unsealing the network guard requires a stated reason")
    with _lock:
        _state["sealed"] = False
        _state["reason"] = reason


def is_sealed() -> bool:
    return bool(_state["sealed"])


def status() -> dict:
    return dict(_state)


@contextmanager
def allow_network(reason: str):
    """Temporarily permit outbound sockets, then re-seal.

    ``reason`` is required and is intended to be written to the operation log
    by the caller so that every unsealing is accounted for.
    """
    was_sealed = is_sealed()
    unseal(reason)
    try:
        yield
    finally:
        if was_sealed:
            seal("re-sealed after: " + reason)
