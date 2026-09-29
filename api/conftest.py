"""Pytest root conftest.

Ensures `api/` (so tests can `import main`) and the vendored
`service_pipeline/` (so `main` can import `tern.cli`) are on sys.path,
independent of whether the venv's editable `.pth` files load — they get
re-hidden by macOS file flags on this checkout, which breaks `site.addpackage`.

Also eagerly pre-imports the transformers symbols our pipeline pulls in
lazily. Reason: transformers ≥ 5.x has a partial-import quirk where the
first `from transformers import AutoModel` after a fresh interpreter can
ImportError even though the package is installed; a second attempt then
succeeds because the module cache has settled. In a normal app run the
uv startup + lifespan setup buys enough time between import attempts that
this is invisible. In pytest, the test fires within ~10 ms of TestClient
init so both attempts hit the same broken state and the test fails
nondeterministically. Doing the (cheap) import at conftest load time
forces the resolution before any test starts.

Also: exclude iCloud-dupe ` 2.py` siblings from pytest collection. These
are Finder's iCloud Drive duplicate-naming artifacts (file foo.py + file
"foo 2.py" appears when iCloud detects a parallel edit). They're NOT
tracked by git (.gitignore filters them out) and NOT
shipped to the bundle (prepare_bundle.sh rsync excludes them), but pytest's default `test_*.py` glob happily collects them
— and they contain stale assertion code from before the canonical
file's last edit. Pytest then reports phantom failures against a file
that isn't tracked or shipped, masking the real test status. Glob-
exclude here so the test runner ignores them too.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE_PIPELINE = HERE.parent / "service_pipeline"

# pytest collect_ignore_glob: skip iCloud-dupe siblings (e.g.,
# "test_foo 2.py", "conftest 2.py"). The pattern matches "<anything> 2.py"
# under any subdir of api/.
collect_ignore_glob = ["* 2.py", "**/* 2.py"]

for p in (HERE, SERVICE_PIPELINE):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

# Eager pre-import: idempotent + cheap (just gates the module-level
# re-export work, no model download). Swallows errors silently so the
# rest of the suite still runs even if transformers is missing entirely
# — the failing test's own pytest.skip catch will report appropriately.
try:
    from transformers import AutoModel, AutoProcessor  # noqa: F401
except Exception:
    pass


# Keep the whole suite out of the developer's real state directory.
#
# The licence-activation tests POST to /api/license/activate with a stubbed
# server that answers is_valid=true, and the handler then writes the result
# to ~/Library/Application Support/Tern/license.json — the real one. That
# left a valid-looking licence on the machine after every run, which is
# quietly dangerous now that the trial quota is enforced: a developer box
# that believes it is licensed skips the gate entirely, so the very bug the
# gate exists to prevent would never show up locally.
#
# The same goes for the structured log: main.py appends to
# ~/Library/Logs/tern-crash.log, so every run used to grow the developer's
# real support log with test noise. It goes to the temp dir as well.
#
# The licence endpoint has no default in the source (a build supplies it
# through TERN_LICENSE_SERVER), so the session points it at a reserved,
# unroutable name. Activation tests replace urlopen, so nothing is ever
# sent; the value only has to be a well-formed endpoint.
#
# Session-scoped and autouse so no individual test has to remember. Tests
# that need a different location (or the real default) override it with the
# function-scoped monkeypatch fixture, which restores this afterwards.
import os
import resource
import shutil
import tempfile
from functools import lru_cache

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_app_state_dir():
    tmp = tempfile.mkdtemp(prefix="tern-test-state-")
    previous = os.environ.get("TERN_STATE_DIR")
    os.environ["TERN_STATE_DIR"] = tmp

    import main
    original_license_file = main._LICENSE_FILE
    original_crash_log = main._CRASH_LOG
    original_license_server = main._LICENSE_DEFAULT_SERVER
    main._LICENSE_FILE = Path(tmp) / "license.json"
    main._CRASH_LOG = Path(tmp) / "tern-crash.log"
    main._LICENSE_DEFAULT_SERVER = "https://licence.example.invalid/functions/v1/license-validate"
    try:
        yield Path(tmp)
    finally:
        main._LICENSE_FILE = original_license_file
        main._CRASH_LOG = original_crash_log
        main._LICENSE_DEFAULT_SERVER = original_license_server
        if previous is None:
            os.environ.pop("TERN_STATE_DIR", None)
        else:
            os.environ["TERN_STATE_DIR"] = previous
        shutil.rmtree(tmp, ignore_errors=True)


# ─── No model downloads ──────────────────────────────────────────────────
# Two licence tests start a real background index to prove the trial gate
# lets it through. On a machine with ffmpeg and whisper-cli but no Whisper
# model, that job would quietly fetch ~550 MB from Hugging Face:
# HF_HUB_OFFLINE does not cover it, because it is a plain urllib download.
# The tests only need the gate's answer, so the download is refused here.
@pytest.fixture(scope="session", autouse=True)
def _no_whisper_model_download():
    import tern.audio as audio

    def _refuse(model_path):
        raise RuntimeError(f"the test suite never downloads the Whisper model ({model_path})")

    original = audio._download_whisper_model
    audio._download_whisper_model = _refuse
    try:
        yield
    finally:
        audio._download_whisper_model = original


# ─── Open-file limit ─────────────────────────────────────────────────────
# Every module-scoped TestClient runs the app lifespan, which opens a Store
# (SQLite plus Chroma's own SQLite and HNSW files), and the suite also opens
# media, thumbnails and temp files. macOS gives a shell a soft limit of 256
# descriptors, which a full run can get close to. Raise the soft limit for
# this process to min(hard, 4096); never lower a limit that is already
# higher, and carry on quietly if the platform refuses.

def _raise_open_file_limit(target: int = 4096) -> None:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(hard, target)
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ValueError, OSError):
        pass


_raise_open_file_limit()


# ─── Tests that need the heavy runtime ───────────────────────────────────
# Most of the suite runs anywhere the locked Python environment is
# installed. A few tests go through the real search engine or the real
# indexing pipeline, and those need things a fresh clone or a CI runner
# does not have. They carry a marker and skip, with the reason, when the
# dependency is missing:
#
#   @pytest.mark.needs_siglip
#       The SigLIP-2 weights must already be in the local Hugging Face
#       cache. The check reads the cache only and never downloads, so it
#       gives the same answer with or without HF_HUB_OFFLINE=1. Anything
#       that calls get_engine() (search, clip export, index start) loads
#       the model and needs this.
#
#   @pytest.mark.needs_tools("ffmpeg", "whisper-cli", "vision-ocr")
#       Each executable must be found the way POST /api/index looks for
#       it: in $TERN_BIN_DIR, then on PATH, and for vision-ocr also at
#       service_pipeline/bin/vision-ocr (built with swiftc, gitignored).
#
# Data-dependent tests (an indexed transcript, the seeded demo DB) skip
# from their own fixtures instead, because only the test knows what data
# it needs.

@lru_cache(maxsize=None)
def _siglip_missing_files() -> tuple[str, ...]:
    """Files of the default embedding model that are not in the local
    Hugging Face cache. Empty when the model can load offline."""
    from tern.models import IngestConfig

    model_id = IngestConfig.model_fields["embedding_model"].default
    if Path(model_id).is_dir():
        return ()
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return ("huggingface_hub",)
    needed = (
        "config.json",
        "model.safetensors",
        "preprocessor_config.json",
        "tokenizer_config.json",
    )
    return tuple(
        name for name in needed
        if not isinstance(try_to_load_from_cache(model_id, name), str)
    )


def _tool_present(name: str) -> bool:
    bundled = os.environ.get("TERN_BIN_DIR", "")
    if bundled and (Path(bundled) / name).exists():
        return True
    if shutil.which(name):
        return True
    return name == "vision-ocr" and (SERVICE_PIPELINE / "bin" / "vision-ocr").exists()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "needs_siglip: needs the SigLIP-2 weights in the local Hugging Face "
        "cache; skipped otherwise",
    )
    config.addinivalue_line(
        "markers",
        "needs_tools(*names): needs these executables (TERN_BIN_DIR or PATH); "
        "skipped otherwise",
    )


def pytest_runtest_setup(item):
    if item.get_closest_marker("needs_siglip"):
        missing = _siglip_missing_files()
        if missing:
            pytest.skip(
                "SigLIP-2 weights are not in the local Hugging Face cache "
                f"(missing: {', '.join(missing)}); start the app once with "
                "network access to download them"
            )
    for marker in item.iter_markers("needs_tools"):
        absent = [name for name in marker.args if not _tool_present(name)]
        if absent:
            pytest.skip(f"needs {', '.join(absent)} (not on PATH or in TERN_BIN_DIR)")
