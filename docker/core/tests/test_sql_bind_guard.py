"""Regression guard: forbid the ``INTERVAL '%s ...'`` SQL bind anti-pattern.

A ``%s`` placeholder *inside* a quoted SQL string literal is NOT a psycopg
parameter marker. Under psycopg3 (server-side binding) the literal renders as
``INTERVAL '$1 ...'`` and the parameter cannot bind, raising at execute time::

    bind message supplies 1 parameters, but prepared statement requires 0

Every site that used this pattern wrapped the call in ``try/except``, so the
failures were *silently swallowed* — the login brute-force IP ban, the tribute
cooldown, and the affection timeline all quietly broke while the unit tests
(which mock the DB connection) stayed green.

The correct form is ``make_interval(mins => %s)`` / ``make_interval(hours => %s)``
/ ``make_interval(days => %s)``.

This guard scans the entire served ``app/`` surface so the bug class can never
silently return. It is intentionally a static check (no live DB needed) because
the original failure was invisible to the mocked behavioral tests.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# A %s / %b / %(name)s appearing INSIDE a quoted INTERVAL literal, e.g.
#   INTERVAL '%s minutes'        INTERVAL '%(n)s days'
_BAD_INTERVAL = re.compile(r"INTERVAL\s+'[^']*%\(?\w*\)?[sb]", re.IGNORECASE)


def test_no_interval_percent_placeholder_bind_antipattern():
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _BAD_INTERVAL.search(line):
                offenders.append(
                    f"{path.relative_to(APP_DIR.parent)}:{lineno}: {line.strip()}"
                )

    assert not offenders, (
        "Found the `INTERVAL '%s ...'` SQL bind anti-pattern. psycopg3 will not "
        "bind the parameter (it lands inside the quoted literal). Use "
        "make_interval(... => %s) instead:\n  " + "\n  ".join(offenders)
    )
