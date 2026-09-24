"""Shared pytest setup for the tern-service suite.

Open-file limit
    Every Store opens SQLite plus Chroma's own SQLite and HNSW files, and
    test_storage.py builds a fresh Store per test. Store.close() releases
    them, but a test that fails before its teardown, or a future fixture
    that forgets to close, should not turn into a wall of "Too many open
    files" errors on macOS, whose shells default to a soft limit of 256.
    The soft limit for this process is raised to min(hard, 4096). A limit
    that is already higher is left alone, and a platform that refuses the
    change is ignored.

@pytest.mark.needs_tools("say", "ffmpeg", ...)
    The test runs real executables. It skips, naming what is missing, when
    any of them is not on PATH: `say` exists only on macOS, and ffmpeg and
    ffprobe are not installed on a fresh Mac or a CI runner.
"""
from __future__ import annotations

import resource
import shutil

import pytest


def _raise_open_file_limit(target: int = 4096) -> None:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(hard, target)
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ValueError, OSError):
        pass


_raise_open_file_limit()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "needs_tools(*names): needs these executables on PATH; skipped otherwise",
    )


def pytest_runtest_setup(item):
    for marker in item.iter_markers("needs_tools"):
        absent = [name for name in marker.args if shutil.which(name) is None]
        if absent:
            pytest.skip(f"needs {', '.join(absent)} on PATH")
