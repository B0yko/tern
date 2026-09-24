# Architecture

Tern indexes a folder of media three ways, then fuses the three signals into
one ranked result list.

## Layout

```
service_pipeline/tern/   the indexing + search library (no HTTP, no UI)
api/main.py              FastAPI wrapper over the library
api/licensing.py         trial quota and machine identity, no FastAPI imports
app/                     frontend — vanilla JS modules, no build step
tauri/src-tauri/         native macOS shell that spawns the API as a sidecar
license_server/          licence validation, a Supabase Edge Function
```

The dependency arrow points one way: `app → api → service_pipeline`. The
library has no idea the HTTP layer exists. That is what lets the pipeline be
tested without starting a server, and what would let the API be rewritten
without touching the UI.

## Indexing

One pass per file, by media kind:

| Stage | Tool | Applies to |
|---|---|---|
| Voice activity detection | Silero VAD | audio, video |
| Speech to text | Whisper Large v3 Turbo Q5 via whisper.cpp | audio, video |
| Keyframe extraction | ffmpeg, scene-threshold plus adaptive interval | video |
| On-screen text | Apple Vision, `service_pipeline/bin/vision-ocr.swift` sidecar | video keyframes, images |
| Scene embedding | SigLIP-2 base patch16-256, 768-dim | video keyframes, images |

VAD is there because Whisper hallucinates on silence. It will confidently
transcribe room tone as speech, and the classic artefacts ("Thank you",
"you") land on silent intros and outros. `audio.py` runs Silero VAD over the
extracted 16 kHz WAV before Whisper starts, but it does not cut the audio:
removing the silent stretches would shift every timestamp away from the
source file. Whisper transcribes the untouched audio, and afterwards each
segment whose midpoint falls outside every detected speech region is
dropped. When VAD fails, finds no speech at all, or is switched off with
`TERN_VAD=0`, nothing is filtered. The surviving word-level segments are
then merged into chunks of about 8 seconds.

The OCR sidecar is a separate Swift binary rather than a Python binding
because Apple Vision is an Objective-C API with no maintained Python wrapper
worth the dependency. It speaks JSONL over stdin and stdout: paths in, one
JSON object per image out. No automated test pins that protocol directly.
`scripts/qa_smoke.py` checks it only indirectly, by expecting hits for slide
text from an index the sidecar produced.

## Storage

Two stores, chosen per query shape:

SQLite holds `files`, `transcript_segments`, `ocr_segments` and `keyframes`,
plus two FTS5 external-content virtual tables, `transcript_fts` and
`ocr_fts`, both using `tokenize='porter unicode61'`. External-content means
the FTS index does not duplicate the text, it points back at the source
table. Porter stemming makes `pricing` match `priced`. `unicode61` keeps
Cyrillic, accented Latin and other space-separated scripts searchable, with
case folding (`привет` matches `Привет`). It does not segment Chinese or
Japanese: a run of CJK characters without spaces becomes a single token, so
`予算` inside `会議の予算について` does not match, and only a prefix of the run
matches through the trailing wildcard. Transcripts are also English by
default: the app never passes a language, so `whisper-cli` runs with
whisper.cpp's default, `en`. `/api/index` and the CLI accept a `language`
for anything else.

ChromaDB holds the SigLIP-2 keyframe embeddings for cosine search. It is a
separate store because the access pattern is unrelated. Approximate nearest
neighbour over 768-dim vectors has nothing in common with an inverted text
index, and forcing both into one engine would compromise both.

### HNSW search breadth

The keyframe collection is created with `ef_search=400` (`HNSW_EF_SEARCH` in
`storage.py`). At Chroma's default of 100 the graph traversal did not
reliably reach every vector. On the author's 430-keyframe demo index the query
`sushi` has exactly one true match, at cosine 0.1280, and the default setting
returned it in 1 of 5 fresh processes. In the other four an unrelated frame
at 0.0471 came back instead, the noise floor then dropped the whole visual
channel, and the search was empty. Which way it fell depended on the
traversal's entry point, which changes per process. At 400 the same probe
found the match 5 times out of 5.

`get_or_create_collection` applies that configuration only when it creates
the collection, so `Store` also raises `ef_search` in place when it opens an
existing collection that is below 400. That is a metadata write, not a
re-index, and a failure there is logged rather than allowed to stop the app
from opening. The other recall lever, `max_neighbors`, is fixed when the
index is built and would need a re-index.

The configuration names the cosine space as well. Chroma 1.x ignores the
legacy `hnsw:space` metadata once a configuration is passed, so without it a
new collection is built in l2: visual scores come back as 1 − L2 distance,
mostly negative, the noise floor never engages, and nearly any query returns
a page of unrelated frames. Existing indexes were unaffected, which is why it
only showed on a clean install. A test pins the space on a fresh store.

## Search

`service_pipeline/tern/search.py` is where the three channels meet.

### Query sanitising

FTS5's `MATCH` syntax is a minefield for real user input. `sanitize_fts_query`
handles the cases that actually showed up:

Hyphens were the worst of them. FTS5 reads the `-19` in `covid-19` as a
column filter, so the query raised `no such column: 19`, the per-channel `try/except` swallowed the error,
and the user got zero text hits with nothing to explain why. The visual
channel still returned something, so the result list never looked broken
enough to report. Stripping the hyphen turns it into an AND, which is what
people mean anyway.

Unbalanced quotes are the same class of problem. Search-as-you-type means that
the moment someone presses `"` the query is syntactically invalid, so an odd
quote count gets a closing quote appended.

The trailing token is expanded to `(token* OR token)` so `Stanf` finds
Stanford before you finish typing. The bare form is OR'd back in because the
index is Porter-stemmed and the wildcard form alone would miss the stem.

### Fusion

Each channel runs independently inside its own `try/except`, so one failure
degrades the result list instead of blanking it; the failure is logged
rather than swallowed. The SigLIP text embedding goes to a background thread
first, because it is the most expensive part of a query. The code's own
estimate is 50–150 ms for the warm embed against 10–30 ms per FTS5 channel,
so running the two side by side hides the FTS5 time under the embed instead
of adding them up. `scripts/qa_smoke.py` holds the whole
request to a p50 under 100 ms and a p99 under 200 ms on Apple Silicon.

Hits are then bucketed by `(file_id, ts_ms // 5000)`. A bucket holding more
than one source is the strongest signal Tern has: speech, on-screen text and
scene all pointing at the same five seconds. Those get a bonus capped at half
the base score. The API marks them `multi`, and the result row badges up
to two of the channels that matched.

### Visual noise floor

SigLIP returns the top N by cosine regardless of absolute score, so a query
that matches nothing still comes back with plausible-looking keyframes. Three
filters:

1. Absolute: below 0.07 is noise on this model.
2. Relative: when a strong match exists, drop anything under half of it.
3. Flat distribution: gibberish like `zxqv_no_match` produces uniformly
   mediocre scores clustered around 0.10, because out-of-vocabulary tokens map
   near a centroid. If the top score is weak *and* the top five are tightly
   grouped, there is no signal, so every visual hit is dropped.

The third filter is the one that matters. The first two on their own still let
nonsense queries come back looking confident.

Filename matches skip the noise floor entirely, since they carry explicit user
intent. That channel exists because SigLIP base is weak on single-word
queries: `mountain` does not reliably retrieve `picsum_mountain.jpg` on cosine
alone. The boost is multiplicative (×1.2) and needs 3 characters, except for
tokens containing CJK, where the threshold drops to 2. 会議 and 회사 are whole
words, and a hardcoded 3-character minimum would quietly exclude the most
common search terms in those languages.

Filename hits are also kept out of the scores that set the floor. The
filename channel returns a flat synthetic 0.50, which means "the basename
matched", not a cosine. When it was folded into the per-bucket visual scores,
it became the top score, the relative floor rose to 0.25, and real SigLIP-2
base cosines, which top out around 0.15, were all culled. `snowy mountain
peak` returned only `picsum_mountain.jpg`, a name match showing the wrong
scene, and dropped the actual snowy peak at 0.1379. Now filename buckets
bypass the floor on their own and the floor is computed from SigLIP scores
only, so the real photo comes back second.

## Export

Exports are the one place Tern writes files a user takes into other tools,
so the writers are defensive.

- **Atomic text exports.** CSV, SRT and FCPXML are written to a temporary
  file in the target directory, fsynced, then moved into place with
  `os.replace` (`_atomic_write_text` in `api/main.py` and in `clip.py`). A
  kill mid-write leaves the old file or the new one, never half of one. MP4
  and MP3 clips are written by ffmpeg straight to their final path.
- **FCPXML escaping.** Every interpolated string goes through one
  `_xml_escape` that handles all five XML-reserved characters, so a project
  or file name with `&` or `"` cannot break the document or inject
  structure. Clip names and marker values are truncated *before* escaping,
  so a length cap can never cut an entity like `&amp;` in half, which Final
  Cut and DaVinci reject as a corrupt import.
- **CSV formula injection.** A cell that starts with `=`, `+`, `-`, `@`, a
  tab or a carriage return, including after leading spaces, gets a leading
  single quote, so a spreadsheet treats it as text rather than a formula.
- **Bounded input.** CSV and FCPXML exports take at most 200 hits, project
  names are capped at 200 characters, and a clip may not be longer than 30
  minutes.
- **Source allowlist.** `/api/export/clip` accepts a source only if it is
  inside the workspace or is an indexed file, the same check `/api/file`
  uses. Without it, exporting an arbitrary file and then downloading the
  result from the workspace would be a way to read any file on the machine.
- **Timeouts.** ffmpeg gets 120 s for an MP3 extract and 300 s for a video
  re-encode, and each ffprobe call during FCPXML generation gets 30 s. A
  timeout comes back as a 504 whose message names the likely cause (a
  corrupt source or a stalled mount) and is logged to `tern-crash.log`.

Video clips are encoded with `h264_videotoolbox` rather than libx264, which
is GPL. `clip.py` records the measurement behind the `-q:v 75` setting: it
matches `x264 -crf 20` on PSNR at about three times the file size.

## Licensing

The pipeline has no notion of licensing. The rules live in
`api/licensing.py`, which is pure and tested directly, and `api/main.py`
decides when to consult them.

**Trial.** An unlicensed copy may index 120 minutes of media in total. It
meters media duration rather than calendar days, and photos are free.
`/api/index` refuses with 402 once the quota is spent. During a run each
file's duration is probed first; a file that does not fit in what is left is
skipped whole rather than half-indexed, and a file is charged only after it
has actually been indexed. The state is a small JSON file written
atomically. An absent file is a fresh trial, but a damaged or reshaped one
counts as fully spent: the gate fails closed. A probe error on a single
file fails open, because a corrupt file is an indexing problem, not a
billing one. The quota is enforced in the API layer only, so the CLI, which
drives the library directly, is not metered.

**Machine identity.** Activation posts the key, the app version and a
machine id to the licence server. The id is the SHA-256 of the Mac's
`IOPlatformUUID` with a fixed `tern:` prefix, so the server can count
machines without learning which Mac is which.

**Seat cap.** `license_server/` is a Supabase Edge Function. The seat rules
are a pure `decide()` in `decide.ts`, covered by `decide_test.ts`: unknown
key, revoked, expired, and a full licence are refused, and a machine that
already holds a seat always re-activates, even when the licence is full.
The `licensing` schema is not exposed through PostgREST; the function
reaches it through two `SECURITY DEFINER` functions, `tern_license_lookup`
and `tern_license_record` (`schema_rpc.sql`), executable only by
`service_role`. `tern_license_record` repeats the seat count before it
inserts, as a backstop, and takes a row lock on the licence first, so two
machines racing for the last seat are serialised and only one of them gets
it.

No endpoint is compiled into the source. The API reads it from
`TERN_LICENSE_SERVER`; an activation request may also name its own
`server_url`, which is how a self-hosted `license_server/` is tested. With
neither, activation answers with a message naming the setting instead of
contacting anything.

The endpoint answers a bad key with 200 and `is_valid: false`, and a
database failure with 503. The client treats any transport failure as
"keep what is cached", so an outage cannot lock out a licensed copy, and it
never re-validates the cached licence on launch. The verdict is not signed
and the cache is a plain JSON file. Like the trial gate, which
`api/licensing.py` describes as Python inside a bundle the user controls,
this stops casual copying, not a determined user.

## Testing

500 tests across three suites: 168 for the pipeline, 319 for the API, and
13 for the licence server's seat rules.

```bash
cd api && uv sync --locked                       # one environment for both Python suites
cd service_pipeline && ../api/.venv/bin/python -m pytest -q
cd api && .venv/bin/python -m pytest -q
deno test license_server/decide_test.ts
```

Run each command from the repository root. The pipeline suite loads no
models; three of its FCPXML tests call macOS `say` and `ffmpeg` to make a
real media file. Parts of the API suite need the SigLIP-2 weights and `ffmpeg`,
`whisper-cli` and `vision-ocr` on the machine, and a few need the indexed
demo workspace from `scripts/init_demo.sh`. Those tests skip, with the
reason, when what they need is missing (`pytest -rs` lists them), so a fresh
clone runs green. CI runs that configuration on a macOS runner, with
`HF_HUB_OFFLINE=1` and an empty `TERN_WORKSPACE`. `scripts/dev_check.sh`
runs both pytest suites plus an end-to-end smoke test against a live
backend.
