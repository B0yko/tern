"""Tern CLI."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .clip import export_fcpxml, extract_audio_clip, extract_clip
from .ingest import Indexer
from .models import IngestConfig, SearchQuery
from .search import SearchEngine
from .storage import Store
from .vision import Embedder


console = Console()


def default_config(workspace: Path = Path.cwd()) -> IngestConfig:
    bin_path = Path(__file__).resolve().parent.parent / "bin" / "vision-ocr"
    model_path = Path(__file__).resolve().parent.parent / "models" / "ggml-large-v3-turbo-q5_0.bin"
    return IngestConfig(
        root_path=workspace,
        db_path=workspace / "db" / "tern.db",
        chroma_path=workspace / "db" / "chroma",
        thumbnails_path=workspace / "db" / "thumbnails",
        whisper_model=str(model_path),
        vision_ocr_binary=str(bin_path),
    )


@click.group()
@click.option("--workspace", "-w", type=click.Path(path_type=Path), default=Path.cwd())
@click.pass_context
def cli(ctx, workspace: Path):
    """Tern — local AI search for podcast & video archives."""
    ctx.ensure_object(dict)
    workspace = workspace.resolve()
    ctx.obj["workspace"] = workspace
    ctx.obj["config"] = default_config(workspace)


@cli.command()
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--language", "-l", default=None, help="Transcription language (e.g. 'en', 'de'). whisper.cpp's default, English, if omitted.")
@click.option("--force", is_flag=True, help="Re-index files that are already done.")
@click.pass_context
def index(ctx, folder: Path, language: str | None, force: bool):
    """Index all video/audio files in FOLDER."""
    config = ctx.obj["config"]
    indexer = Indexer(config)
    console.print(f"[bold]Tern[/bold] indexing [cyan]{folder}[/cyan]")
    console.print(f"  Whisper model: {config.whisper_model}")
    console.print(f"  Embedding model: {config.embedding_model}")
    console.print(f"  Database: {config.db_path}")
    console.print()
    results = indexer.index_directory(folder, language=language, force=force)
    console.print()
    console.print(f"[bold green]Done[/bold green] indexed={results['indexed']} skipped={results['skipped']} errors={results['errors']} time={results['elapsed_s']:.1f}s")
    indexer.close()


@cli.command()
@click.argument("query")
@click.option("--limit", "-n", default=20, help="Max results.")
@click.option("--source", "-s", multiple=True, type=click.Choice(["transcript", "ocr", "visual"]), default=None)
@click.option("--json-output", "-j", is_flag=True, help="Output as JSON.")
@click.pass_context
def search(ctx, query: str, limit: int, source, json_output: bool):
    """Search the indexed archive."""
    config = ctx.obj["config"]
    store = Store(config.db_path, config.chroma_path)
    embedder = Embedder(model_name=config.embedding_model)
    engine = SearchEngine(store, embedder)
    sources = list(source) if source else ["transcript", "ocr", "visual"]
    q = SearchQuery(query=query, limit=limit, sources=sources)
    hits = engine.search(q)

    if json_output:
        # ensure_ascii=False — fifth and final site of the JSON-escape
        # bug pattern after storage.py, log_event
        # and the license cache. Search hits carry user-
        # transcribed text (Cyrillic podcasts, CJK videos, etc.) AND
        # filenames in arbitrary scripts. A scripting user piping
        # `tern search ... --json-output | jq` would otherwise see
        # `"snippet":"\\u0414\\u043e\\u043a..."` instead of the
        # readable original — fine for re-parsing but useless for
        # eyeball inspection. Python's `print` on a UTF-8 terminal
        # (locale.getpreferredencoding() = UTF-8 on macOS) renders
        # the canonical form correctly.
        print(json.dumps([h.model_dump() for h in hits], indent=2, default=str, ensure_ascii=False))
        return

    if not hits:
        console.print(f"[yellow]No results for '{query}'[/yellow]")
        return

    table = Table(title=f"Results for '{query}' ({len(hits)} hits)")
    table.add_column("#", width=3)
    table.add_column("File", style="cyan", no_wrap=False, max_width=40)
    table.add_column("Time", style="green")
    table.add_column("Src", style="magenta")
    table.add_column("Score", justify="right")
    table.add_column("Snippet", no_wrap=False, max_width=60)

    for i, hit in enumerate(hits, 1):
        ts_str = f"{hit.ts_ms//60000:02d}:{(hit.ts_ms//1000)%60:02d}"
        table.add_row(
            str(i),
            Path(hit.file_path).name,
            ts_str,
            hit.source,
            f"{hit.score:.2f}",
            hit.snippet[:120],
        )

    console.print(table)
    store.close()


@cli.command()
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.argument("start_ms", type=int)
@click.argument("end_ms", type=int)
@click.option("--output", "-o", type=click.Path(path_type=Path), required=True)
@click.option("--audio-only", is_flag=True, help="Extract audio only (mp3).")
@click.option("--padding-ms", default=1500, help="Padding around clip (ms).")
def clip(file_path: Path, start_ms: int, end_ms: int, output: Path, audio_only: bool, padding_ms: int):
    """Extract a clip from a source file."""
    if audio_only:
        result = extract_audio_clip(file_path, output, start_ms, end_ms, padding_ms)
    else:
        result = extract_clip(file_path, output, start_ms, end_ms, padding_ms)
    console.print(f"[green]✓[/green] {result}")


@cli.command()
@click.argument("query")
@click.option("--project-name", default="Tern Search Results")
@click.option("--output", "-o", type=click.Path(path_type=Path), required=True)
@click.option("--limit", "-n", default=20)
@click.pass_context
def export(ctx, query: str, project_name: str, output: Path, limit: int):
    """Export search results as FCPXML for Premiere / DaVinci Resolve / Final Cut."""
    config = ctx.obj["config"]
    store = Store(config.db_path, config.chroma_path)
    embedder = Embedder(model_name=config.embedding_model)
    engine = SearchEngine(store, embedder)
    hits = engine.search(SearchQuery(query=query, limit=limit))
    if not hits:
        console.print(f"[yellow]No hits to export.[/yellow]")
        return
    result = export_fcpxml(hits, project_name, output)
    console.print(f"[green]✓[/green] FCPXML written: {result}")
    console.print(f"  Drag this into Premiere or Resolve to load clips on the timeline.")
    store.close()


@cli.command()
@click.pass_context
def stats(ctx):
    """Show indexing statistics."""
    config = ctx.obj["config"]
    store = Store(config.db_path, config.chroma_path)
    s = store.stats()
    table = Table(title="Tern Archive Stats", show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right", style="bold")
    table.add_row("Files (total)", str(s["files_total"]))
    table.add_row("Files (indexed)", str(s["files_done"]))
    table.add_row("Total duration", f"{s['total_duration_ms']/3600000:.1f} hours")
    table.add_row("Transcript segments", str(s["transcript_segments"]))
    table.add_row("OCR blocks", str(s["ocr_segments"]))
    table.add_row("Keyframes (visual)", str(s["keyframes"]))
    console.print(table)
    store.close()


@cli.command()
@click.argument("client_name")
@click.option("--output-dir", "-o", type=click.Path(path_type=Path), default=Path("delivery"))
@click.option("--example-queries", "-q", multiple=True, default=())
@click.pass_context
def deliver(ctx, client_name: str, output_dir: Path, example_queries: tuple):
    """Package the indexed archive into a standalone folder.

    The folder keeps the workspace layout (db/tern.db, db/chroma,
    db/thumbnails), so `tern -w <folder> search ...` works on it
    directly. CLIENT_NAME only titles the generated README.
    """
    config = ctx.obj["config"]
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Copy db files
    import shutil

    db_out = output_dir / "db"
    db_out.mkdir(exist_ok=True)
    if config.db_path.exists():
        shutil.copy(config.db_path, db_out / "tern.db")
    if config.chroma_path.exists():
        shutil.copytree(config.chroma_path, db_out / "chroma", dirs_exist_ok=True)
    if config.thumbnails_path.exists():
        # Copy at most 1,000 thumbnails to keep the package size sane
        thumbs_out = db_out / "thumbnails"
        thumbs_out.mkdir(exist_ok=True)
        for i, p in enumerate(config.thumbnails_path.rglob("*.jpg")):
            if i >= 1000:
                break
            rel = p.relative_to(config.thumbnails_path)
            (thumbs_out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(p, thumbs_out / rel)

    # Generate example clips
    if example_queries:
        clips_out = output_dir / "example_clips"
        clips_out.mkdir(exist_ok=True)
        store = Store(config.db_path, config.chroma_path)
        embedder = Embedder(model_name=config.embedding_model)
        engine = SearchEngine(store, embedder)
        for i, query in enumerate(example_queries[:5]):
            hits = engine.search(SearchQuery(query=query, limit=1))
            if not hits:
                continue
            hit = hits[0]
            safe_name = "".join(c if c.isalnum() else "_" for c in query)[:40]
            out_file = clips_out / f"{i+1:02d}_{safe_name}.mp4"
            try:
                extract_clip(Path(hit.file_path), out_file, hit.ts_ms, hit.ts_ms + 30000)
                console.print(f"  [green]✓[/green] {out_file.name}")
            except Exception as e:
                console.print(f"  [red]✗[/red] {query}: {e}")
        store.close()

    # Write a README describing the package
    readme = output_dir / "README.md"
    readme.write_text(_client_readme(client_name))
    console.print(f"[green]✓[/green] Delivery package: {output_dir}")


def _client_readme(client_name: str) -> str:
    return f"""# Tern archive index — {client_name}

This folder holds a Tern index of an audio/video archive. The index was
built locally; no audio or video was uploaded anywhere to make it.

## What's in this folder

- `db/` — the searchable index, in the same layout as a Tern workspace
  - `tern.db` — SQLite with transcripts, on-screen text (OCR) and file metadata
  - `chroma/` — visual embeddings index
  - `thumbnails/` — keyframe thumbnails (at most 1,000 are copied)
- `example_clips/` — up to 5 sample clips, one per example query, if any were given
- `README.md` — this file

The index stores absolute paths to the original media files. Playback,
clip export and FCPXML export only work on a machine where the media
is still at those paths.

## How to search it

With the Tern command line (`service_pipeline/` in the Tern source tree),
point the workspace at this folder:

```
tern -w /path/to/this/folder search "your query here"
tern -w /path/to/this/folder search "screen showing a pricing slide" -s ocr
tern -w /path/to/this/folder stats
```

### Export clips for Premiere / DaVinci Resolve / Final Cut

```
tern -w /path/to/this/folder export "moment about pricing strategy" -o clips.fcpxml
```

Import the `.fcpxml` into Final Cut Pro, Premiere or Resolve and the
matched clips appear on a timeline with their in and out points.
"""


if __name__ == "__main__":
    cli()
