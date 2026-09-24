"""Tern data models."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class FileRecord(BaseModel):
    """Represents an indexed media file."""

    id: int | None = None
    path: str
    mime: str
    duration_ms: int
    size_bytes: int
    mtime: float
    indexed_at: datetime | None = None
    status: Literal["pending", "indexing", "done", "error"] = "pending"
    error: str | None = None
    metadata: dict | None = None  # EXIF for images; placeholder for future video/audio meta


class TranscriptSegment(BaseModel):
    """One spoken-text segment from Whisper."""

    file_id: int
    start_ms: int
    end_ms: int
    text: str
    confidence: float = 1.0


class OCRSegment(BaseModel):
    """One OCR block from Apple Vision on a single keyframe."""

    file_id: int
    frame_ts_ms: int
    text: str
    confidence: float
    bbox: list[float]  # [x, y, w, h] normalized


class Keyframe(BaseModel):
    """One keyframe extracted from a video for visual indexing."""

    file_id: int
    ts_ms: int
    thumbnail_path: str
    embedding_dim: int


class SearchHit(BaseModel):
    """A single search result."""

    file_id: int
    file_path: str
    ts_ms: int
    duration_ms: int
    snippet: str | None = None
    # "multi" appears after _reweight_and_dedupe merges co-located hits from
    # multiple sources. `sources` carries the actual contributing list so the
    # UI can render a "speech + on-screen" badge instead of bare "multi".
    source: Literal["transcript", "ocr", "visual", "multi"]
    sources: list[str] = Field(default_factory=list)
    score: float
    thumbnail_path: str | None = None


class SearchQuery(BaseModel):
    """A search request."""

    query: str
    limit: int = 50
    sources: list[Literal["transcript", "ocr", "visual"]] = Field(
        default_factory=lambda: ["transcript", "ocr", "visual"]
    )
    file_filter: list[int] | None = None
    weights: dict[str, float] = Field(
        default_factory=lambda: {"transcript": 0.4, "ocr": 0.2, "visual": 0.4}
    )


class IngestConfig(BaseModel):
    """Configuration for an indexing run."""

    root_path: Path
    db_path: Path
    chroma_path: Path
    thumbnails_path: Path
    whisper_model: str = "models/ggml-large-v3-turbo-q5_0.bin"
    whisper_binary: str = "whisper-cli"
    ffmpeg_binary: str = "ffmpeg"
    vision_ocr_binary: str = "bin/vision-ocr"
    embedding_model: str = "google/siglip2-base-patch16-256"
    scene_threshold: float = 0.3
    max_keyframes_per_file: int = 300
    ocr_confidence_threshold: float = 0.5
    # Apple Vision OCR mode: "fast" (~135ms/frame) or "accurate" (~337ms/frame).
    # Accurate fixes diacritic noise (Y Combìnator → Y Combinator), captures more
    # blocks (e.g. faint subtitle text), and ships real confidence scores. 2.5× slower
    # is fine at the volumes we ship to in v0.
    ocr_mode: str = "accurate"
    n_workers: int = 1
    languages: list[str] = Field(default_factory=lambda: ["en", "de"])
