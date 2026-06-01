"""The access-log token-redaction filter scrubs ``?token=...`` so the WS bearer
token (passed as a query param) never sits in plaintext access logs."""
import logging

from app.main import _RedactTokenLogFilter


def _record(msg, args):
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, msg, args, None
    )


def test_redacts_token_in_path_arg():
    f = _RedactTokenLogFilter()
    rec = _record(
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", "/ws?token=secret-abc123&x=1", "1.1", 101),
    )
    assert f.filter(rec) is True
    assert "secret-abc123" not in rec.args[2]
    assert "token=***" in rec.args[2]
    assert "x=1" in rec.args[2]  # other query params preserved


def test_leaves_non_token_args_untouched():
    f = _RedactTokenLogFilter()
    rec = _record("%s %s", ("GET", "/api/messages?limit=50"))
    f.filter(rec)
    assert rec.args == ("GET", "/api/messages?limit=50")


def test_handles_non_tuple_args_without_crashing():
    f = _RedactTokenLogFilter()
    rec = _record("plain message", None)
    assert f.filter(rec) is True
