"""Tests for tern.vision Embedder text-embedding LRU cache.

The full Embedder requires loading SigLIP-2 (~600 MB, ~10 s on first run)
which is too heavy for unit tests. We exercise the cache layer directly
by constructing an Embedder via __new__ (skipping __init__'s model load)
and manually wiring just the cache + processor stubs. The cache logic is
pure Python — model integration is verified by qa_smoke / live runs.
"""
from __future__ import annotations

from collections import OrderedDict

import numpy as np
import pytest

from tern.vision import Embedder


@pytest.fixture
def naked_embedder(monkeypatch):
    """An Embedder shell with cache state initialised but NO model loaded.
    embed_text() will call _text_features which we patch to a deterministic
    stand-in (hash → float vector) so we can assert cache hits vs misses
    by counting invocations."""
    emb = Embedder.__new__(Embedder)
    emb._text_cache = OrderedDict()
    emb._text_cache_max = 4   # small so we can hit eviction in a few calls
    emb.device = "cpu"

    # Counter for how many times the "model" got invoked. Cache hits should
    # leave this unchanged across consecutive same-input calls.
    call_count = {"n": 0}

    # Patch the heavy bits. processor returns a no-op dict; _text_features
    # returns a deterministic 4-dim torch-like array we can convert.
    class _StubProcessor:
        def __call__(self, text=None, **kw):
            class _Inputs:
                def to(self, device): return self
            return _Inputs()
    emb.processor = _StubProcessor()

    def _stub_text_features(_inputs):
        call_count["n"] += 1
        # Need a torch tensor-like with .norm and slicing. Use a real
        # torch tensor — small enough to be cheap.
        import torch
        # Seed from current call_count so each call produces a unique vector
        # if cache were broken (helps detect false cache "hits").
        torch.manual_seed(call_count["n"])
        return torch.rand(1, 4)
    monkeypatch.setattr(emb, "_text_features", _stub_text_features)

    return emb, call_count


def test_embed_text_cache_hit_skips_model_call(naked_embedder):
    """Calling embed_text twice with the same query should invoke the
    model ONCE — second call hits the cache."""
    emb, calls = naked_embedder

    v1 = emb.embed_text("pricing strategy")
    n_after_first = calls["n"]
    v2 = emb.embed_text("pricing strategy")

    assert calls["n"] == n_after_first, f"expected model not re-invoked, got {calls['n']} (was {n_after_first})"
    # Values must match (cache returns a copy of the cached vector).
    np.testing.assert_array_equal(v1, v2)


def test_embed_text_cache_returns_copy_not_alias(naked_embedder):
    """Mutating a returned vector must NOT corrupt the cached entry."""
    emb, _ = naked_embedder
    v1 = emb.embed_text("orange cat")
    v1[0] = 99.0  # mutate caller's copy

    v2 = emb.embed_text("orange cat")
    assert v2[0] != 99.0, "cache leaked a shared reference — caller mutation corrupted it"


def test_embed_text_cache_evicts_oldest_at_max(naked_embedder):
    """Cache is sized to 4 in the fixture. Insert 5 distinct queries;
    the first one must be evicted, the rest must remain."""
    emb, calls = naked_embedder
    queries = [f"q{i}" for i in range(5)]
    for q in queries:
        emb.embed_text(q)
    # 5 distinct queries → 5 model calls so far
    assert calls["n"] == 5

    # q0 was evicted (oldest); calling it again should re-invoke the model
    emb.embed_text("q0")
    assert calls["n"] == 6, "q0 should have been evicted and re-computed"

    # q4 (most-recently-added before this re-add) should still be cached
    n_before = calls["n"]
    emb.embed_text("q4")
    assert calls["n"] == n_before, "q4 should still be cached"


def test_embed_text_cache_move_to_end_on_access(naked_embedder):
    """Cache uses LRU (move-to-end on access), not FIFO. Touching an
    old entry should protect it from eviction."""
    emb, calls = naked_embedder
    emb.embed_text("q0")  # oldest
    emb.embed_text("q1")
    emb.embed_text("q2")
    emb.embed_text("q3")
    # Touch q0 — should move it to MRU
    emb.embed_text("q0")
    # Now insert one more — q1 should be evicted, NOT q0
    emb.embed_text("q4")

    n_before = calls["n"]
    emb.embed_text("q0")  # still cached (protected by recent access)
    assert calls["n"] == n_before, "q0 was incorrectly evicted despite recent access"

    emb.embed_text("q1")  # was evicted
    assert calls["n"] == n_before + 1, "q1 should have been evicted as LRU"


def test_embed_text_recovers_from_concurrent_eviction_race(naked_embedder, monkeypatch):
    """Cache lookup races with cache eviction across
    threads. The sequence that's now defended against:

      Thread A: cached = self._text_cache.get(text)  →  returns value
      Thread B (parallel search):
                popitem(last=False) evicts text
      Thread A: self._text_cache.move_to_end(text)  →  KeyError

    Pre-fix, the KeyError propagated out of embed_text, got caught by
    search.py's per-channel try/except, and logged a noisy traceback
    while skipping the visual channel for that search. Now: targeted
    `try/except KeyError` falls through to the cold-path recompute —
    user gets correct embedding, no log noise.

    We simulate the race by populating the cache, then patching
    move_to_end to raise KeyError once (mimicking the post-eviction
    state). embed_text should NOT raise; it should recompute and
    return a valid vector. call_count should increment (recompute
    happened) and the recomputed value should be cached on
    subsequent same-input calls (post-race recovery).
    """
    emb, calls = naked_embedder

    # Prime the cache so the .get() returns a hit
    v1 = emb.embed_text("orange cat")
    n_after_first = calls["n"]

    # Patch move_to_end to raise KeyError EXACTLY ONCE — simulates
    # a concurrent thread evicting the entry between our .get() and
    # the move_to_end call.
    original_move_to_end = emb._text_cache.move_to_end
    raise_count = {"n": 0}
    def _raise_once(key, *a, **kw):
        if raise_count["n"] == 0:
            raise_count["n"] += 1
            # Simulate the race: actually evict the key before raising,
            # so the recompute path correctly observes the missing
            # entry (otherwise the next call would still cache-hit and
            # we wouldn't exercise the recompute path).
            try: del emb._text_cache[key]
            except KeyError: pass
            raise KeyError(key)
        return original_move_to_end(key, *a, **kw)
    monkeypatch.setattr(emb._text_cache, "move_to_end", _raise_once)

    # This call hits the race: .get() returns the cached value, but
    # move_to_end raises KeyError. The implementation catches the
    # KeyError and falls through to the cold-path recompute. Must
    # NOT raise an exception.
    v2 = emb.embed_text("orange cat")

    # Recompute happened (call_count incremented past n_after_first)
    assert calls["n"] == n_after_first + 1, (
        f"recompute should have fired after concurrent-eviction race; "
        f"call_count went from {n_after_first} to {calls['n']} (expected +1)"
    )
    # raise_once fired exactly once (sanity check on our test stub)
    assert raise_count["n"] == 1, (
        f"race-simulator should have raised KeyError exactly once, got {raise_count['n']}"
    )
    # v2 must be a valid embedding (numpy array, not None / partial)
    assert v2 is not None
    assert hasattr(v2, "shape"), f"expected numpy array, got {type(v2)}"

    # Post-race the cache should be repopulated — subsequent same-input
    # call hits the cache (no further recompute).
    n_before_third = calls["n"]
    v3 = emb.embed_text("orange cat")
    assert calls["n"] == n_before_third, (
        "third call should hit the cache (race recovery repopulated it)"
    )
    np.testing.assert_array_equal(v2, v3)


# ─── embed_images per-path error tolerance ─────────────────────────────
# A truncated keyframe (partial ffmpeg write), a moved file (cleanup race),
# or a permission-denied subdir USED to abort the whole video's visual
# indexing — Image.open in an eager list comprehension threw, the
# exception escaped to ingest.index_file's outer try/except, and the
# WHOLE source file got marked "errored" — losing the Whisper transcript
# + OCR work that had already succeeded for the same file. Per-path
# try/except now skips bad frames while keeping the rest.


def _naked_image_embedder(monkeypatch):
    """Like naked_embedder but stubs the image-side bits instead of text.
    embed_images() will go through the real loop (including the per-path
    Image.open) but the model call gets replaced with a deterministic
    stand-in so we can run without SigLIP loaded."""
    import torch

    emb = Embedder.__new__(Embedder)
    emb.embedding_dim = 4
    emb.device = "cpu"

    class _StubProcessor:
        def __call__(self, images=None, **kw):
            # transformers returns a BatchEncoding with .to(device); the
            # only thing our code does with the result is feed it into
            # _image_features (which we also stub), so the shape and
            # contents don't matter beyond being a passthrough.
            n = len(images) if images is not None else 0
            class _Inputs:
                def to(self_inner, device):
                    return self_inner
            inp = _Inputs()
            inp._n = n
            return inp
    emb.processor = _StubProcessor()

    def _stub_image_features(inputs):
        # Return one row per input image — that's what the real SigLIP
        # image encoder does. Random vectors normalized by the caller.
        n = inputs._n
        return torch.rand(n, 4)
    monkeypatch.setattr(emb, "_image_features", _stub_image_features)

    return emb


def test_embed_images_skips_unreadable_path(monkeypatch, tmp_path, caplog):
    """Mix one valid image with one nonexistent path. The bad path must
    be skipped + logged; the valid one must still get embedded. Pre-fix,
    Image.open(nonexistent) raised FileNotFoundError out of the eager
    list comprehension and the whole call failed."""
    import logging as _logging
    from PIL import Image as _PILImage

    emb = _naked_image_embedder(monkeypatch)

    # One real on-disk image so PIL has something to actually decode.
    good_path = tmp_path / "good.png"
    _PILImage.new("RGB", (8, 8), (255, 0, 0)).save(good_path)
    bad_path = tmp_path / "does_not_exist.jpg"  # never created

    with caplog.at_level(_logging.WARNING, logger="tern.vision"):
        surviving, embeddings = emb.embed_images([good_path, bad_path])

    assert surviving == [good_path], (
        f"only the readable path should survive; got {surviving}"
    )
    assert embeddings.shape == (1, 4), f"one row per surviving path; got shape {embeddings.shape}"
    # Diagnostic log mentioning the skipped path so the user can find it
    # in tern-debug.log after a partial-file warning lands.
    skip_records = [
        r for r in caplog.records
        if r.name == "tern.vision" and str(bad_path) in r.getMessage()
    ]
    assert skip_records, (
        f"expected warning naming the skipped path; got: "
        f"{[(r.name, r.getMessage()) for r in caplog.records]}"
    )


def test_embed_images_all_unreadable_returns_empty(monkeypatch, tmp_path):
    """Edge: every path fails (e.g., entire subdir got moved between
    discover and embed). Function must NOT raise — returns ([],
    zeros(0,D)) so the caller's `for ... in zip(...)` loop just yields
    nothing and the file finishes with zero keyframes (no visual hits
    but no error either)."""
    emb = _naked_image_embedder(monkeypatch)
    bad1 = tmp_path / "ghost1.jpg"
    bad2 = tmp_path / "ghost2.jpg"
    surviving, embeddings = emb.embed_images([bad1, bad2])
    assert surviving == []
    assert embeddings.shape == (0, 4)


def test_embed_images_empty_input_unchanged():
    """No paths in → no work, empty result. Quick guard against a future
    refactor that drops the empty-input early-return and burns processor
    setup on an empty batch."""
    emb = Embedder.__new__(Embedder)
    emb.embedding_dim = 4
    surviving, embeddings = emb.embed_images([])
    assert surviving == []
    assert embeddings.shape == (0, 4)
