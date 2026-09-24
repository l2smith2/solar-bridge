"""Shared fixtures."""
from __future__ import annotations

import socket

import pytest


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def free_port() -> int:
    return _free_port()


@pytest.fixture
def port_factory():
    return _free_port
