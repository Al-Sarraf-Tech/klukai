"""Regression guard: the memory-image READ path must match the WRITE path.

`memory_archive_query.get_image_bytes()` reads files from `IMAGES_DIR`; images
are written by `memory_archive.py` to ITS `IMAGES_DIR` (the companion-images
volume mount, /images). These were out of sync — the query module hardcoded a
nonexistent `/data/images`, so every get_image_bytes() returned None and the
memory archive displayed no drawings even though all the files were present.
"""

from __future__ import annotations


def test_image_read_path_matches_write_path():
    from app import memory_archive, memory_archive_query

    assert memory_archive_query.IMAGES_DIR == memory_archive.IMAGES_DIR, (
        "memory-image READ path (memory_archive_query.IMAGES_DIR) must equal the "
        "WRITE path (memory_archive.IMAGES_DIR) and the companion-images volume "
        "mount — a drift here silently hides every drawing in the archive."
    )
