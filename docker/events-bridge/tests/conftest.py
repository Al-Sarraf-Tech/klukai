"""Isolated bridge harness: never imports core fixtures or connects services."""
import importlib.util
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

# pika is runtime-only on core developer hosts. Transport doubles below exercise
# bridge logic; the shim supplies only the message-properties value container.
try:
    import pika  # noqa: F401
except ModuleNotFoundError:
    sys.modules['pika'] = SimpleNamespace(BasicProperties=lambda **kw: SimpleNamespace(**kw))
os.environ['RABBIT_PASS'] = 'unit-test-unused'
spec = importlib.util.spec_from_file_location('bridge', Path(__file__).parents[1] / 'bridge.py')
bridge = importlib.util.module_from_spec(spec)
sys.modules['bridge'] = bridge
spec.loader.exec_module(bridge)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('bridge tests must not connect to a service')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
