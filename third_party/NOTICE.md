# Third-party components in Tern

No binary has been distributed from this repository. This notice is written
for any app bundle built from it: `scripts/prepare_bundle.sh` copies it, with
the licence texts, into `resources/licenses/` inside the bundle. LGPL 2.1
requires that the user of such a build be told which covered libraries are
present, be given the licence text, and be able to obtain the source and
substitute their own build.

## FFmpeg — LGPL 2.1 or later

FFmpeg 8.0.1, built from unmodified upstream source with GPL components
disabled. Licence text: `ffmpeg/COPYING.LGPLv2.1`.

Configuration:

```
--enable-shared --disable-static
--disable-gpl --disable-nonfree --disable-version3
--disable-libx264 --disable-libx265 --disable-libxvid --disable-libvidstab
--enable-videotoolbox --enable-audiotoolbox --enable-libmp3lame
```

Three obligations, and how each is met:

**Tell the user.** This file, copied into the bundle next to the licence
texts. The app does not link to it from an About window yet; a build that is
distributed needs that link.

**Give them the source.** The build uses unmodified FFmpeg 8.0.1 as published
at `https://ffmpeg.org/releases/ffmpeg-8.0.1.tar.xz`
(sha256 `05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41`).
The same tarball is kept in this repository at `ffmpeg/ffmpeg-8.0.1.tar.xz`,
and `scripts/build_ffmpeg_lgpl.sh` is the exact recipe that turns it into
the bundled binaries. Anyone who asks gets both; the written offer below is
the standing version of that.

**Let them replace it.** The FFmpeg libraries ship as separate `.dylib` files
under `Contents/Resources/resources/libs/`, linked at run time through
`@executable_path/../libs/`. A user can build their own FFmpeg 8.0.1 with the
same soname versions, drop it in, and the app will use it. This is why the
build must stay dynamically linked — statically linking would remove the
right the licence exists to protect.

## LAME — LGPL 2.0 or later

`libmp3lame` 3.100, used for MP3 export. `scripts/build_ffmpeg_lgpl.sh`
links FFmpeg against the Homebrew build (`brew install lame`) and copies
`libmp3lame.0.dylib` into the staged libraries; LAME's source is not in this
repository. It is published at `https://lame.sourceforge.io/`, and the
Homebrew `lame` formula is the build recipe.

LAME is distributed under the GNU Library General Public License, version 2
or (at your option) any later version, which is what the header of `lame.h`
says. Licence text: `lame/COPYING` (the GNU LGPL 2.0 text from gnu.org).
`lame/README-LICENSE` is LAME's own short note on commercial use. It is not
the licence, but it asks users to acknowledge LAME and link to the project's
website, which this section does.

Same three obligations, met the same way: it is a separate `.dylib` beside
the FFmpeg ones and is replaceable.

## Whisper / whisper.cpp / ggml — MIT

Speech recognition. `whisper-cli` and the ggml libraries and backends it
loads come from Homebrew's `whisper-cpp` and `ggml`, and the model weights
are downloaded from Hugging Face (`ggerganov/whisper.cpp`); none of them is
in this repository. `scripts/prepare_bundle.sh` copies the binary, the ggml
dylibs and the backend plugins into a bundle. Permissive; attribution only.

## CPython 3.11 — Python Software Foundation License

The bundled interpreter is the uv-managed CPython 3.11 build, copied whole
by `scripts/prepare_bundle.sh`, so its `LICENSE.txt` travels with it.
Permissive; attribution only.

## SigLIP-2 — Apache 2.0

Scene embeddings, via Hugging Face Transformers
(`google/siglip2-base-patch16-256`, downloaded on first use; not in this
repository). Permissive; attribution only.

## Silero VAD — MIT

Speech detection before transcription, via the `silero-vad` Python package,
which carries its own model weights. Permissive; attribution only.

## ChromaDB, Transformers, sentence-transformers — Apache 2.0; PyTorch — BSD-3-Clause

Vector store and model runtime, installed from PyPI. Permissive. Each
package's licence file is in its `*.dist-info` directory, which
`scripts/prepare_bundle.sh` copies along with the rest of `site-packages`.

## Inter, Inter Tight, JetBrains Mono — SIL Open Font License 1.1

The interface fonts, vendored as woff2 subsets in `app/fonts/`:

| Font | Copyright | Licence text |
|---|---|---|
| Inter | 2016 The Inter Project Authors | `app/fonts/LICENSE-Inter.txt` |
| Inter Tight | 2022 The Inter Project Authors | `app/fonts/LICENSE-InterTight.txt` |
| JetBrains Mono | 2020 The JetBrains Mono Project Authors | `app/fonts/LICENSE-JetBrainsMono.txt` |

The OFL allows bundling the fonts with software as long as the copyright
notice and the licence travel with them and the fonts are not sold on their
own. The licence texts sit next to the font files, so they go into the
bundle with the rest of `app/`.

## pillow-heif — must be resolved before any distribution

`pillow-heif` decodes HEIC/HEIF photos. Its source is BSD-3-Clause, but its
macOS binary wheels bundle libheif and libde265 (LGPL 3) and x265 (GPL 2),
and the wheel's own licence summary calls the binary wheel GPLv2 for that
reason. Checked in pillow-heif 1.3.0: `pillow_heif/.dylibs/` contains
`libx265.215.dylib`.

`scripts/prepare_bundle.sh` copies the whole api `site-packages` into the
bundle, so a bundle built today would carry libx265 even though the FFmpeg
build leaves it out. Before any build is distributed, pillow-heif has to
be replaced with a decoder that does not link x265 (pi-heif, the decode-only
variant, is the obvious candidate once its bundled licences are checked),
and `prepare_bundle.sh` should refuse to package a `libx265` the same way it
already refuses a GPL ffmpeg.

## What is deliberately absent

**libx264 and libx265 are not in the FFmpeg build.** They are GPL. Including
them in a proprietary application would require Tern itself to be
distributed under the GPL, which its all-rights-reserved licence does not
allow. H.264 encoding uses Apple's
VideoToolbox instead, which is licensed as part of macOS.

If a future build ever needs x264 — and it should not — that decision changes
what licence this product can be distributed under. It is not a performance
tuning choice.

---

## Written offer

> The FFmpeg and LAME libraries in any build of this software distributed
> from this repository are covered by the GNU Lesser General Public License,
> version 2.1 or later (FFmpeg), and the GNU Library General Public License, version 2
> or later (LAME). For at least three years from the date you received such
> a build, we will provide, on request, a complete machine-readable copy of
> the corresponding source code for those libraries, together with the
> scripts used to configure and build them.
>
> No binary has been distributed from this repository so far. The offer
> applies to any build that is. Ask by opening an issue at
> https://github.com/B0yko/tern/issues.
