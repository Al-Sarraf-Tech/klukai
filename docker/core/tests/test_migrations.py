"""Fail before acquiring a database connection for unusable migration inputs."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.migrations import apply_migrations


@pytest.mark.parametrize("kind", ["missing", "empty", "file", "no_sql"])
async def test_invalid_directory_fails_before_database_access(tmp_path, kind):
    directory = tmp_path / "migrations"
    if kind in {"empty", "no_sql"}:
        directory.mkdir()
    if kind == "no_sql":
        (directory / "README").write_text("No migrations")
    if kind == "file":
        directory.write_text("not a directory")
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("database must not be touched")
    with pytest.raises(RuntimeError, match="migration|Migration"):
        await apply_migrations(pool, directory)


async def test_invalid_utf8_fails_before_database_access(tmp_path: Path):
    (tmp_path / "001.sql").write_bytes(b"SELECT '\xff';")
    pool = MagicMock()
    pool.connection.side_effect = AssertionError("database must not be touched")
    with pytest.raises(UnicodeDecodeError):
        await apply_migrations(pool, tmp_path)
