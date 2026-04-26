"""Tests for the file-retention beat tasks."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


@pytest.fixture()
def populated_dirs(tmp_path):
    """Create a small directory tree with a mix of old and recent files.

    Returns a dict with paths and the expected old-file count.
    """
    root = tmp_path / "uploads"
    root.mkdir()
    sub = root / "tenant-a"
    sub.mkdir()

    old = root / "stale.pdf"
    old.write_bytes(b"x" * 100)
    old_sub = sub / "old-amendment.pdf"
    old_sub.write_bytes(b"y" * 200)
    fresh = root / "fresh.pdf"
    fresh.write_bytes(b"z" * 50)

    # Backdate the two "old" files to ~120 days ago.
    cutoff = time.time() - (120 * 86400)
    os.utime(old, (cutoff, cutoff))
    os.utime(old_sub, (cutoff, cutoff))

    return {"root": root, "old_files": [old, old_sub], "fresh": fresh}


def test_cleanup_removes_only_old_files(populated_dirs):
    from app.workers.tasks import _cleanup_dir

    result = _cleanup_dir(
        populated_dirs["root"],
        max_age_days=90,
        kind="uploads",
    )
    assert result["removed"] == 2
    assert result["bytes"] == 300
    for f in populated_dirs["old_files"]:
        assert not f.exists(), f"{f} should have been removed"
    assert populated_dirs["fresh"].exists(), "fresh file must survive"
    # Empty subdir should be cleaned up too.
    assert not (populated_dirs["root"] / "tenant-a").exists()


def test_cleanup_no_op_when_disabled(populated_dirs):
    from app.workers.tasks import _cleanup_dir

    result = _cleanup_dir(
        populated_dirs["root"],
        max_age_days=0,
        kind="uploads",
    )
    assert result["removed"] == 0
    assert all(f.exists() for f in populated_dirs["old_files"])


def test_cleanup_handles_missing_root(tmp_path):
    from app.workers.tasks import _cleanup_dir

    result = _cleanup_dir(
        tmp_path / "does-not-exist",
        max_age_days=30,
        kind="uploads",
    )
    assert result == {"removed": 0, "bytes": 0}
