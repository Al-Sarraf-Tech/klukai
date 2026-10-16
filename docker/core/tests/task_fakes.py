"""Task admission doubles that explicitly dispose of work outside test scope."""

from unittest.mock import MagicMock


def discard_task(coroutine):
    """Record admission through the mock while closing intentionally unrun work."""
    coroutine.close()
    return MagicMock()
