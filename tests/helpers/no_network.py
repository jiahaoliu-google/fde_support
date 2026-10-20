"""Blocks outbound network connections for the unit-test suite.

Imported (for its side effect) by iap_test_utils, db_test_utils and the test modules that
exercise outbound clients. A test that forgets to patch an agent call would otherwise
reach the real Agent Runtime with the developer's gcloud credentials, and could still
pass because the backend escalates to a human when an agent call fails.

Only loopback hosts and Unix sockets are allowed. A blocked call is printed to stderr
as ``BLOCKED NETWORK CALL`` *and* raised, so it shows up in the test output even when
the code under test swallows the exception.

Scope: this guards Python-level sockets (httpx, requests, urllib, google-auth token
refresh). Libraries with their own C networking stack (gRPC core) are not covered.
"""
import socket
import sys

ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "testserver"})

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo
_real_create_connection = socket.create_connection


class NetworkBlockedError(OSError):
    """Raised when a unit test tries to open a non-loopback network connection."""


def _block(host, port):
    msg = (f"BLOCKED NETWORK CALL to {host}:{port} - unit tests must not reach real services; "
           f"patch the client (e.g. backend.clients.agent_runtime.<name>) instead.")
    print(msg, file=sys.stderr, flush=True)
    raise NetworkBlockedError(msg)


def _check_address(family, address):
    if family not in (socket.AF_INET, socket.AF_INET6):
        return  # Unix sockets etc.
    if isinstance(address, tuple) and address:
        host = str(address[0])
        if host not in ALLOWED_HOSTS:
            _block(host, address[1] if len(address) > 1 else None)


def _guarded_connect(self, address):
    _check_address(self.family, address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    _check_address(self.family, address)
    return _real_connect_ex(self, address)


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    if host is not None:
        name = host.decode() if isinstance(host, bytes) else str(host)
        if name not in ALLOWED_HOSTS:
            _block(name, port)
    return _real_getaddrinfo(host, port, *args, **kwargs)


def _guarded_create_connection(address, *args, **kwargs):
    host = str(address[0]) if address else None
    if host is not None and host not in ALLOWED_HOSTS:
        _block(host, address[1] if len(address) > 1 else None)
    return _real_create_connection(address, *args, **kwargs)


def install():
    """Idempotently installs the guard for the whole process."""
    if getattr(socket.socket.connect, "_fde_no_network", False):
        return
    for fn in (_guarded_connect, _guarded_connect_ex, _guarded_getaddrinfo, _guarded_create_connection):
        fn._fde_no_network = True
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.create_connection = _guarded_create_connection


install()
