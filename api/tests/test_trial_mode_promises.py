"""Build-time invariant: every search-box placeholder example must
match SOMETHING in the bundled demo workspace.

WHY THIS TEST EXISTS
====================

The rotating placeholder above the search input (topbar.js
PLACEHOLDER_EXAMPLES) is the FIRST thing a new user sees that
suggests "this is the kind of thing you can search for." If a
user copies one of those examples into the search box during
the trial and gets ZERO results, the immediate conclusion is "OCR
doesn't work" / "visual search doesn't work" / "this app is
broken."

Commit ea761cc fixed exactly that bug for "slide with $29/month" —
a placeholder that cued OCR as a feature but matched zero content
in the demo workspace. This test pins the invariant so neither:

    (a) a future PLACEHOLDER_EXAMPLES edit (add a new example
        without checking the demo content), nor
    (b) a future demo-workspace edit (remove the slide / podcast
        / photo that backs an existing placeholder),

can silently re-introduce that failure mode.

For each non-prompt example (the literal "Find any moment…" prompt
opener is skipped) the test checks that the demo workspace has at
least one match via ANY of:

  1. transcript_fts (FTS5) — matched by Whisper transcript text
  2. ocr_fts (FTS5) — matched by Apple Vision OCR text
  3. any file basename contains a query token (proxies for
     search_filename's filename-match channel, which compensates
     for SigLIP's weaker single-word visual queries; also a
     reasonable proxy for whether the visual-channel SigLIP
     cosine match has SOMETHING semantically aligned to find —
     a "mountain lake" placeholder that has no file named
     anything like "mountain" probably also won't fire visually)

A "zero across all three" failure is the precise condition that
trial-killed the previous "slide with $29/month" example. We do
NOT run SigLIP in this test (would require model load) — the
filename-channel proxy is the right tradeoff: cheap to run, and
the only placeholders that pass filename-channel-only are the
visual ones whose demo content we DO know exists.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
TOPBAR_JS = REPO_ROOT / "app" / "modules" / "topbar.js"
ONBOARDING_JS = REPO_ROOT / "app" / "modules" / "onboarding.js"
LICENSE_JS = REPO_ROOT / "app" / "modules" / "license.js"
README_MD = REPO_ROOT / "README.md"
DEMO_DB = REPO_ROOT / "demo" / "db" / "tern.db"

# Where the app sends people for anything that needs the author: licence
# requests, support, bug reports. Single source of truth — every in-app
# surface that links out for these MUST agree. Tern has no store, so no
# surface may point at a checkout, a price or a mail address instead.
CANONICAL_ISSUES_URL = "https://github.com/B0yko/tern/issues"
# Where a failed in-app update sends the user to fetch a build by hand.
CANONICAL_RELEASES_URL = "https://github.com/B0yko/tern/releases"

# The literal first example is the prompt opener, not a query — skip it.
_PROMPT_OPENER = "Find any moment…"


def _parse_placeholder_examples(source: str) -> list[str]:
    """Pull the PLACEHOLDER_EXAMPLES = [ ... ] array out of topbar.js
    as a list of raw strings. Simple regex parser since the array is
    a flat list of string literals — no nested expressions, no
    template literals."""
    m = re.search(r"const PLACEHOLDER_EXAMPLES\s*=\s*\[(.*?)\];",
                  source, re.DOTALL)
    if not m:
        raise AssertionError(
            "couldn't locate PLACEHOLDER_EXAMPLES in topbar.js — was the "
            "constant renamed? Update _parse_placeholder_examples."
        )
    body = m.group(1)
    # Pick out double-quoted string literals (the array uses ", not ').
    strings = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
    # Decode only the JS escapes the array actually uses: \\ → \ and
    # \" → ". The earlier impl used `.encode("utf-8").decode("unicode_
    # escape")` which corrupts multi-byte UTF-8 chars (`…` in "Find any
    # moment…" → `â\x80¦`) because unicode_escape's input model is
    # Latin-1, not UTF-8. The substitution-pair approach below leaves
    # the raw UTF-8 bytes intact while still handling the backslash-
    # quote case the regex's `\\.` capture lets through.
    return [s.replace(r'\\', '\\').replace(r'\"', '"') for s in strings]


def _basename_tokens(path: str) -> set[str]:
    """Mimic storage.search_filename's token derivation: lowercase the
    stem, split on _-.space, return the resulting word set."""
    stem = Path(path).stem.lower()
    return set(re.sub(r"[_\-.\s]+", " ", stem).split())


@pytest.fixture(scope="module")
def demo_db():
    if not DEMO_DB.exists():
        pytest.skip(
            f"demo workspace DB not present at {DEMO_DB} — this test runs "
            f"against the bundled demo content; skip outside the dev tree."
        )
    conn = sqlite3.connect(f"file:{DEMO_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # The file alone proves nothing: when TERN_WORKSPACE is unset, the first
    # TestClient lifespan in the run creates demo/db/tern.db with the full
    # schema and no rows. Only a DB with indexed files is the seeded demo.
    # A missing `files` table is not "unseeded", it is schema drift, so that
    # OperationalError is left to fail the test.
    if conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0:
        conn.close()
        pytest.skip(
            f"demo workspace DB at {DEMO_DB} has no indexed files — fetch "
            f"the demo media and run scripts/init_demo.sh to seed it."
        )
    yield conn
    conn.close()


def _has_transcript_hit(conn: sqlite3.Connection, query: str) -> bool:
    # Build a simple LIKE check per token. FTS5 MATCH would be faster
    # but requires the query to be FTS5-safe; LIKE is robust enough
    # for the trial-promise check and avoids replicating
    # sanitize_fts_query here.
    tokens = [t for t in re.findall(r"\w+", query.lower()) if len(t) >= 2]
    if not tokens:
        return False
    for t in tokens:
        row = conn.execute(
            "SELECT 1 FROM transcript_segments WHERE LOWER(text) LIKE ? LIMIT 1",
            (f"%{t}%",),
        ).fetchone()
        if row:
            return True
    return False


def _has_ocr_hit(conn: sqlite3.Connection, query: str) -> bool:
    tokens = [t for t in re.findall(r"\w+", query.lower()) if len(t) >= 2]
    if not tokens:
        return False
    for t in tokens:
        row = conn.execute(
            "SELECT 1 FROM ocr_segments WHERE LOWER(text) LIKE ? LIMIT 1",
            (f"%{t}%",),
        ).fetchone()
        if row:
            return True
    return False


def _has_filename_token_match(conn: sqlite3.Connection, query: str) -> bool:
    """Proxy for search_filename's basename-token check. Token >=3
    chars (matches storage.search_filename's Latin filter; CJK 2-char
    carve-out doesn't apply for any current placeholder)."""
    tokens = [t for t in re.findall(r"\w+", query.lower()) if len(t) >= 3]
    if not tokens:
        return False
    rows = conn.execute("SELECT path FROM files").fetchall()
    for r in rows:
        base_words = _basename_tokens(r["path"])
        for t in tokens:
            if t in base_words:
                return True
    return False


def test_every_placeholder_example_matches_demo_workspace(demo_db):
    """For every PLACEHOLDER_EXAMPLES entry except the prompt opener,
    the demo workspace must have at least one match via transcript,
    OCR, or filename channel. A failure here means a user typing the
    placeholder during trial would see zero results — the exact
    sellability-kill failure mode commit ea761cc closed."""
    source = TOPBAR_JS.read_text(encoding="utf-8")
    examples = _parse_placeholder_examples(source)
    assert examples, "couldn't parse any PLACEHOLDER_EXAMPLES — parser broken?"

    failures: list[str] = []
    for ex in examples:
        if ex == _PROMPT_OPENER:
            continue  # not a query
        transcript = _has_transcript_hit(demo_db, ex)
        ocr = _has_ocr_hit(demo_db, ex)
        fname = _has_filename_token_match(demo_db, ex)
        if not (transcript or ocr or fname):
            failures.append(
                f"  {ex!r}: zero matches in demo workspace "
                f"(transcript={transcript}, ocr={ocr}, filename={fname})"
            )

    if failures:
        raise AssertionError(
            "trial-killing placeholder(s): a user types one in the search "
            "box during the trial demo and gets ZERO results — exactly the "
            "regression commit ea761cc closed. Fix by either:\n"
            "  (1) replacing the placeholder with one that DOES match the "
            "bundled demo (sqlite3 demo/db/tern.db 'SELECT … LIKE …%' to "
            "find a working candidate), or\n"
            "  (2) adding demo content that backs the placeholder.\n\n"
            "Offending placeholders:\n"
            + "\n".join(failures)
        )


def _parse_onboarding_sub_strings(source: str) -> list[str]:
    """Pull the `sub: "..."` field out of each entry in the onboarding
    STEPS array. Returns the raw sub strings. The STEPS array contains
    object literals like `{ title: "...", sub: "...", icon: "..." }` —
    we match the sub key directly rather than parsing the whole array."""
    # `sub:` followed by a double-quoted string. Same escape rules as
    # the topbar parser (\\ and \" inside the literal).
    raw = re.findall(r'sub:\s*"((?:[^"\\]|\\.)*)"', source)
    return [s.replace(r'\\', '\\').replace(r'\"', '"') for s in raw]


def _extract_scare_quoted_phrases(sub: str) -> list[str]:
    """A sub string may embed example phrases as scare-quoted snippets
    like 'try \"orange cat\" or \"mountain lake\"'. After the JS-escape
    decode the \" became " in the live string. We pull those out so the
    main test can verify each is backed by the demo. Returns [] if the
    sub is purely descriptive."""
    # Match anything between paired " in the live string. The sub
    # itself has already had \" → " converted by the parser, so we
    # look for live double-quotes.
    return re.findall(r'"([^"]+)"', sub)


def test_onboarding_scare_quoted_phrases_match_demo(demo_db):
    """Mirror of test_every_placeholder_example_matches_demo_workspace
    for the first-run onboarding tour. The tour's three subtitles
    describe the speech / OCR / visual search channels. If a subtitle
    embeds a scare-quoted phrase as an example (e.g. `Find the slide
    with "$29/month"`), a trial user is highly likely to copy that
    phrase into the search box right after dismissing the tour — same
    sellability-kill failure mode the topbar placeholders are pinned
    against.

    Original onboarding copy had THREE such trial-killers on screen 2
    alone ("$29/month", "guest's name", "GitHub URL") and three more
    on screen 3 ("orange cat", "mountain lake", "person at a
    whiteboard"), all zero-match in the bundled demo. Current copy
    avoids scare-quoted phrases entirely — the test enforces that any
    FUTURE re-introduction of one MUST be backed by demo content.
    """
    source = ONBOARDING_JS.read_text(encoding="utf-8")
    subs = _parse_onboarding_sub_strings(source)
    assert subs, "couldn't parse any onboarding STEPS subs — parser broken?"

    failures: list[str] = []
    for sub in subs:
        for phrase in _extract_scare_quoted_phrases(sub):
            transcript = _has_transcript_hit(demo_db, phrase)
            ocr = _has_ocr_hit(demo_db, phrase)
            fname = _has_filename_token_match(demo_db, phrase)
            if not (transcript or ocr or fname):
                failures.append(
                    f"  {phrase!r} (in sub: {sub[:60]!r}…): zero matches "
                    f"in demo workspace (transcript={transcript}, "
                    f"ocr={ocr}, filename={fname})"
                )

    if failures:
        raise AssertionError(
            "trial-killing scare-quoted phrase(s) in onboarding tour: a "
            "user reads the tour, copies the quoted phrase into the "
            "search box, and gets ZERO results — same failure mode the "
            "topbar placeholders are pinned against. Fix by either:\n"
            "  (1) removing the scare-quotes (describe the use-case "
            "without seeding a specific query), or\n"
            "  (2) replacing with a phrase that DOES match the bundled "
            "demo.\n\n"
            "Offending phrases:\n" + "\n".join(failures)
        )


def test_onboarding_parser_self_check():
    """The onboarding parser correctly extracts sub strings and
    scare-quoted phrases. Pinned against synthetic input so the main
    onboarding test can't silently pass vacuously after a parser
    refactor."""
    src = '''
        const STEPS = [
          { title: "A", sub: "first sub", icon: "x" },
          { title: "B", sub: "second sub with \\"quoted\\" phrase", icon: "y" },
        ];
    '''
    subs = _parse_onboarding_sub_strings(src)
    assert subs == ["first sub", 'second sub with "quoted" phrase'], subs

    phrases = _extract_scare_quoted_phrases('try "orange cat" or "mountain"')
    assert phrases == ["orange cat", "mountain"], phrases

    # A sub with no scare-quotes returns no phrases.
    assert _extract_scare_quoted_phrases("just a plain description") == []


def test_license_modal_offers_licence_request_path_when_unlicensed():
    """Commit 677bccf put an inline call to action in app/modules/
    license.js so a trial user who wants a key doesn't have to close
    the modal and go looking for where to get one. Tern has no store,
    so the CTA is a "Request a licence" link to the project's issue
    tracker. This test pins that CTA so a future refactor of
    license.js can't silently drop it.

    Three invariants:
      (1) license.js references the canonical issues URL,
      (2) the CTA carries the "Request a licence" label,
      (3) the CTA only appears in the not-yet-licensed branch of
          _open() — it must NOT render for an already-licensed user.
    """
    src = LICENSE_JS.read_text(encoding="utf-8")
    assert CANONICAL_ISSUES_URL in src, (
        f"license.js no longer references {CANONICAL_ISSUES_URL} — the "
        f"inline licence-request CTA appears to have been removed or the "
        f"URL was changed. Trial users opening the license modal now "
        f"have no path to a key. Restore the link, or update "
        f"CANONICAL_ISSUES_URL above if the canonical URL moved (and "
        f"audit the other surfaces that reference it)."
    )
    assert "Request a licence" in src, (
        "license.js no longer carries the 'Request a licence' label on "
        "the CTA. If the wording changed deliberately, update this pin."
    )
    # The CTA block must be guarded by a status === "active" check
    # so already-licensed users don't see a "Request a licence" link.
    # We look for the conditional pattern that wraps the CTA — if
    # someone refactored license.js to render the CTA unconditionally,
    # this assertion catches it.
    assert 'status === "active" ? ""' in src, (
        "license.js licence-request CTA appears to no longer be gated "
        "by license status — the pattern was "
        '`${status && status.status === "active" ? "" : `<cta>`}` '
        "so already-licensed users don't see a redundant 'Request a "
        "licence' link. If you refactored the conditional, update "
        "this test to match the new gating pattern."
    )


def test_in_app_contact_links_are_consistent_and_non_commercial():
    """Every in-app surface that sends the user somewhere for help, a
    licence or a build must point at the SAME canonical GitHub pages,
    and none may point at a store, a price or a mail address on a
    domain the project doesn't own.

    Currently expected surfaces:
      - app/modules/license.js (the "Request a licence" CTA)
      - app/modules/keyhelp.js (the ⌘/ overlay's Support link)
      - app/modules/prefs.js   (the "Diagnostics copied" toast)
      - app/modules/updater.js (the update-failed banner → releases)

    If a new surface starts linking out, add it below.
    """
    modules = REPO_ROOT / "app" / "modules"
    issues_bare = CANONICAL_ISSUES_URL.split("://", 1)[1]
    releases_bare = CANONICAL_RELEASES_URL.split("://", 1)[1]
    expected = {
        "license.js": issues_bare,
        "keyhelp.js": issues_bare,
        "prefs.js": issues_bare,
        "updater.js": releases_bare,
    }
    drift: list[str] = []
    for name, needle in expected.items():
        path = modules / name
        if not path.exists():
            drift.append(f"  app/modules/{name}: file missing")
            continue
        if needle not in path.read_text(encoding="utf-8"):
            drift.append(f"  app/modules/{name}: no reference to {needle}")

    # Nothing user-facing in the app may carry the old storefront
    # copy: a checkout link, a euro price, a mailto on an unowned
    # domain.
    forbidden = [
        (re.compile(r"gumroad", re.IGNORECASE), "a Gumroad link"),
        (re.compile(r"tern\.fm", re.IGNORECASE), "the unregistered storefront domain"),
        (re.compile(r"mailto:", re.IGNORECASE), "a mailto: link"),
        (re.compile(r"€"), "a euro price"),
        (re.compile(r"Buy a licen[cs]e", re.IGNORECASE), "a Buy CTA"),
    ]
    app_files = sorted((REPO_ROOT / "app").rglob("*.js")) + \
        sorted((REPO_ROOT / "app").glob("*.html"))
    for path in app_files:
        text = path.read_text(encoding="utf-8")
        for pat, what in forbidden:
            for m in pat.finditer(text):
                line_no = text[: m.start()].count("\n") + 1
                drift.append(
                    f"  {path.relative_to(REPO_ROOT)}:{line_no}: {what} "
                    f"({m.group(0)!r})"
                )

    if drift:
        raise AssertionError(
            "In-app contact / licence / download links drifted. Every "
            "surface must use CANONICAL_ISSUES_URL (or "
            "CANONICAL_RELEASES_URL for builds), and none may carry "
            "storefront copy.\n\n" + "\n".join(drift)
        )


def _parse_readme_try_searching_queries(source: str) -> list[str]:
    """Pull every backticked query out of the README's `Try searching:`
    bullet list. Each bullet can carry one or more backticked phrases
    (the `mountain / beach / office` bullet has three). Stops at the
    first blank line after the section header so we don't accidentally
    pull backticks from other sections."""
    lines = source.splitlines()
    in_section = False
    queries: list[str] = []
    for line in lines:
        if line.strip().startswith("Try searching"):
            in_section = True
            continue
        if not in_section:
            continue
        # End of section: blank line or non-bullet block.
        if not line.strip():
            break
        if not line.lstrip().startswith("-"):
            break
        for q in re.findall(r"`([^`]+)`", line):
            queries.append(q)
    return queries


def test_readme_try_searching_queries_match_demo(demo_db):
    """Every backticked query in the README's `Try searching:` list
    MUST match SOMETHING in the bundled demo workspace. Same failure
    mode as test_every_placeholder_example_matches_demo_workspace —
    a reader follows the README's quick-start section, types one of
    the suggested queries verbatim, and either gets the promised hits
    or walks away thinking the app is broken.

    What prompted this test: "solo founder loneliness" and "stanford
    student" were either zero-match or weakly matched against the
    bundled demo, so the README examples returned nothing — the same
    pattern as the topbar placeholder and onboarding tour bugs
    already pinned in this file."""
    source = README_MD.read_text(encoding="utf-8")
    queries = _parse_readme_try_searching_queries(source)
    assert queries, (
        "couldn't find any backticked queries under the README's "
        "`Try searching:` section — parser broken or the section was "
        "removed / renamed?"
    )

    # Some examples are explicitly demoing the visual-scene channel
    # (SigLIP-2 cosine match against keyframe embeddings) and have no
    # transcript / OCR / filename presence by design — that's the whole
    # point of the example. The test can't load SigLIP (would require
    # ~600 MB of model weights + GPU), so we manually allowlist queries
    # known to work via the visual channel against the bundled demo.
    # Whoever adds a new visual-only README example MUST add it here
    # AND verify by hand that it returns a hit in the live UI.
    _VISUAL_ONLY_ALLOWLIST = {
        "butterfly",  # SigLIP match on big_buck_bunny.mp4 keyframes
    }

    failures: list[str] = []
    for q in queries:
        if q in _VISUAL_ONLY_ALLOWLIST:
            continue
        transcript = _has_transcript_hit(demo_db, q)
        ocr = _has_ocr_hit(demo_db, q)
        fname = _has_filename_token_match(demo_db, q)
        if not (transcript or ocr or fname):
            failures.append(
                f"  {q!r}: zero matches in demo workspace "
                f"(transcript={transcript}, ocr={ocr}, filename={fname})"
            )

    if failures:
        raise AssertionError(
            "README `Try searching:` example(s) zero-match the bundled "
            "demo workspace — a reader following the quick-start section "
            "and typing the example verbatim gets ZERO results, same "
            "failure as commits ea761cc / 2e9e574. Fix by either:\n"
            "  (1) replacing the query with one that DOES match the "
            "demo (sqlite3 demo/db/tern.db 'SELECT … LIKE …%' to find "
            "a working candidate), or\n"
            "  (2) adding demo content that backs the example.\n\n"
            "Offending queries:\n" + "\n".join(failures)
        )


def test_demo_workspace_file_count_consistent_across_docs(demo_db):
    """Commit 1b97dfa reconciled a "19 files / ~4 h" drift across the
    docs (the real demo was 17 files / ~29 min at the time). The drift
    mechanic is mechanical: someone re-bundles the demo (adds/removes
    a sample file), updates ONE surface, forgets the others. A reader
    sees one number in the README and another in the sidebar.

    Pin: every doc that claims a demo-workspace file count MUST agree
    with the SQL-truth from demo/db/tern.db. The regex below matches
    "N files", "N-file", "N pre-indexed files" (the three phrasings
    actually in use); if a future copy introduces a different
    phrasing, extend the pattern."""
    import re

    actual_count = demo_db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert actual_count > 0, (
        f"demo workspace at {DEMO_DB} reports 0 files — bundled "
        "demo DB is empty or schema drifted"
    )

    # Surfaces that mention the demo-workspace count. Add new ones
    # here if another doc starts citing it.
    surfaces = [
        README_MD,
    ]

    # "17 files", "17-file", "17 pre-indexed files" — the phrasings
    # currently in use. Captures N to compare against canonical.
    pattern = re.compile(
        r"\b(\d{1,3})[- ](?:file|pre-indexed\s+file)s?\b",
        re.IGNORECASE,
    )

    failures: list[str] = []
    for surface in surfaces:
        if not surface.exists():
            continue
        text = surface.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            claimed = int(m.group(1))
            # Only flag claims that obviously refer to the demo
            # workspace, not unrelated numbers (e.g. "100 files" as
            # a benchmark figure). Heuristic: a 50-char window
            # before the match mentions "demo" or "workspace" or
            # "ships" or "pre-indexed" or "library".
            ctx_start = max(0, m.start() - 80)
            ctx = text[ctx_start:m.end()].lower()
            if not any(
                kw in ctx for kw in ("demo", "workspace", "ships", "pre-indexed", "bundled")
            ):
                continue
            if claimed != actual_count:
                failures.append(
                    f"  {surface.relative_to(REPO_ROOT)}: claims "
                    f"'{m.group(0)}' but demo DB has {actual_count}"
                )
    if failures:
        raise AssertionError(
            f"demo-workspace file count drifted from the SQL-truth "
            f"({actual_count} files in demo/db/tern.db). A reader "
            "of the docs then opening the .app sees a different "
            "number in the sidebar. Fix by either updating the "
            "surface to the canonical count, or re-bundling the "
            "demo if the surface's number is the intended one. "
            "Same drift commit 1b97dfa fixed.\n\n"
            "Offending surfaces:\n" + "\n".join(failures)
        )


def test_empty_state_keeps_media_kind_breakdown():
    """Commit 4fd45fe added an "11 audio · 3 video · 5 images" line
    under the library count in the day-N empty state. The breakdown
    is the one moment a user staring at "19 files in library"
    learns Tern handles audio AND video AND photos in one search.
    Without the breakdown, that "this finds my photos too?" moment
    never lands.

    The line reads three fields off state.stats: files_audio,
    files_video, files_image. /api/stats serves them; if either side
    drops a field (frontend or backend), the breakdown silently
    disappears with no explicit error. Pin both ends:

      - empty.js references all three field names
      - empty.js gates the breakdown render on ≥2 kinds present
        (a single-kind library should NOT render "11 audio" — that
        adds nothing the count line doesn't already convey; if the
        gate disappears, the rationale comment on the gate is the
        thing to update, not silently re-enable the noisy variant)
    """
    empty_js = (REPO_ROOT / "app" / "modules" / "empty.js").read_text(encoding="utf-8")

    for field in ("files_audio", "files_video", "files_image"):
        assert field in empty_js, (
            f"empty.js no longer references state.stats.{field} — "
            f"the commit 4fd45fe media-kind breakdown line is gone "
            f"(or the backend field was renamed). Without it, the "
            f"day-N library header loses the all-media-types line. "
            f"Restore the field reference OR update "
            f"this test if /api/stats changed shape (and update the "
            f"frontend to match)."
        )

    # Gate condition — keep the "≥2 kinds present" floor so a
    # single-kind library doesn't render a redundant solo breakdown.
    assert "kindParts.length >= 2" in empty_js, (
        "empty.js breakdown gate appears to have been changed — the "
        "commit 4fd45fe rationale was to ONLY render the audio/video/"
        "image breakdown when ≥2 kinds present, so a single-kind "
        "library doesn't get a redundant '11 audio' line that adds "
        "nothing the count already conveys. If you're widening the "
        "gate intentionally, update this test alongside the change."
    )


def test_prefs_keeps_replay_onboarding_tour_button():
    """docs/TROUBLESHOOTING.md (commit 432a34e) names the button
    verbatim: "click Preferences → Advanced → 'Replay onboarding
    tour…'". A future refactor that simplifies the Advanced section
    by removing the button silently breaks that cross-reference —
    a user reading the doc looks for the literal label, finds
    nothing, gives up on replaying the tour (the alternative is
    manually deleting tern.onboarded.v1 from localStorage via the
    WebView Inspector, which most users won't do).

    Pin the load-bearing pieces in prefs.js:
      - the button id (#prefs-replay-onboarding) — referenced
        implicitly by the doc's instruction sequence
      - the visible label ("Replay onboarding tour…") — the doc
        names it verbatim with the trailing ellipsis
      - the localStorage.removeItem("tern.onboarded.v1") call
        — without it the button no-ops (the tour wouldn't
        re-show on next launch because the flag survives)
    """
    src = (REPO_ROOT / "app" / "modules" / "prefs.js").read_text(encoding="utf-8")
    assert "prefs-replay-onboarding" in src, (
        "prefs.js no longer has the Replay onboarding button "
        "(#prefs-replay-onboarding gone). The troubleshooting "
        "page's localStorage section names this button verbatim — "
        "users reading the doc can't find it. Restore the button "
        "OR update docs/TROUBLESHOOTING.md to point at the new UI."
    )
    assert "Replay onboarding tour" in src, (
        "prefs.js no longer carries the 'Replay onboarding tour' "
        "label literal. TROUBLESHOOTING.md names it verbatim with "
        "the trailing ellipsis. Restore the label OR update the doc."
    )
    assert "tern.onboarded.v1" in src, (
        "prefs.js no longer removes tern.onboarded.v1 from "
        "localStorage — the button might still render but clicking "
        "it would no-op (onboarding flag survives, tour doesn't "
        "replay next launch). Restore the removeItem call."
    )


def test_prefs_keeps_copy_diagnostics_for_support_button():
    """Commit 1551942 added the "Copy diagnostics for support…"
    button to the prefs popover's Advanced section. Commit a9096d8
    then surfaced it at the top of the troubleshooting doc (now
    docs/TROUBLESHOOTING.md) as the FIRST step before filing an
    issue ("open Preferences → Advanced → Copy diagnostics for
    support…").

    Two surfaces now depend on the button existing:
      - the troubleshooting page intro (broken cross-reference if
        the button disappears — a user follows the doc, can't
        find the button, gives up or files a content-free report
        that wastes a round-trip)
      - the support-runbook workflow itself (every "what does
        their diagnostics look like?" thread starts here)

    A future "clean up the Advanced section" refactor that
    removes the button would silently break both. Pin the
    load-bearing identifiers:
      - the button id (#prefs-copy-diagnostics) and label
        ("Copy diagnostics for support…")
      - the handler fetching /api/diagnostics and copying to
        clipboard
    """
    src = (REPO_ROOT / "app" / "modules" / "prefs.js").read_text(encoding="utf-8")
    assert "prefs-copy-diagnostics" in src, (
        "prefs.js no longer has the Copy diagnostics button (id="
        "'prefs-copy-diagnostics' is gone). The troubleshooting "
        "page's first-step instruction now points at a phantom UI "
        "element — users following the doc can't find it. Restore "
        "the button OR update docs/TROUBLESHOOTING.md to point at "
        "the new surface."
    )
    assert "Copy diagnostics for support" in src, (
        "prefs.js no longer carries the literal 'Copy diagnostics "
        "for support' label. Users reading the troubleshooting "
        "doc's instruction to 'click Copy diagnostics for support…' "
        "now scan the prefs popover and find no matching label. "
        "Restore the label OR update docs/TROUBLESHOOTING.md."
    )
    assert '"/api/diagnostics"' in src, (
        "prefs.js no longer fetches /api/diagnostics — the Copy "
        "diagnostics button may still be there visually but it's "
        "not doing the work. Restore the fetch call."
    )
    assert "navigator.clipboard.writeText" in src, (
        "prefs.js no longer writes diagnostics to the clipboard. "
        "The button might appear to do something (toast fires) "
        "but the user pastes nothing into their report. "
        "Restore the clipboard write."
    )


def test_license_modal_toasts_on_activate_and_clear_success():
    """Commit 81e0ffc added success toasts on Activate ("Tern
    activated — thanks!") and Remove license ("License removed") so
    the user gets non-local confirmation that their click landed.

    Previously the only feedback was the modal re-rendering with
    the new state block — local feedback that only lands if the
    user is STILL looking at the modal. The actual flow is:

      paste key → Activate → Cmd-Tab back to the email to file the
      receipt → return to Tern wondering "did anything happen?"

    Toasts survive that focus switch. Pin both the import and the
    two flashToast calls so a future refactor of license.js that
    "cleans up unused imports" or rewrites the activate/clear
    handlers can't silently drop the user-visible confirmation.
    """
    src = LICENSE_JS.read_text(encoding="utf-8")
    assert 'from "/modules/toast.js"' in src, (
        "license.js no longer imports flashToast — the commit 81e0ffc "
        "Activate / Remove success toasts are gone. Restore the "
        "import OR update this test if the toast plumbing moved."
    )
    assert "Tern activated" in src, (
        "license.js no longer fires the activate success toast. A "
        "user who Cmd-Tabbed away during the request now has no "
        "way to know their click landed. Restore the flashToast call "
        "in the activate success branch."
    )
    assert "License removed" in src, (
        "license.js no longer fires the clear success toast. Same "
        "non-local-confirmation rationale as the activate toast. "
        "Restore the flashToast call in the clear success branch."
    )


def test_sidebar_subscribes_to_license_state_changes():
    """Commit 7be77e6 fixed a real user-visible bug: the sidebar's
    "Trial mode" → "Licensed" badge wasn't reactive. After a
    successful activation:
       1. License modal updates with state.license = active
       2. Toast says "Tern activated — thanks!" (commit 81e0ffc)
       3. Sidebar STAYS on "Trial mode" until some unrelated event
          (next keystroke, bookmark, etc.) triggers a re-render

    The user sees the toast + modal say Licensed, then looks at the
    sidebar and sees Trial. Trust friction at the exact moment
    commits 677bccf + 81e0ffc were designed to remove.

    The one-line fix was adding `if (k === "license") _render();`
    to the sidebar's subscribe callback. A future "tidy up
    subscriber callbacks" refactor could silently drop it and the
    bug recurs, hidden behind the fact that the badge DOES update
    on next-render via any unrelated state change.

    Pin asserts sidebar.js subscribes to the license key. If the
    pattern moves (e.g., centralised state-to-render mapping),
    update this assertion to match."""
    src = (REPO_ROOT / "app" / "modules" / "sidebar.js").read_text(encoding="utf-8")
    assert 'k === "license"' in src, (
        "sidebar.js no longer re-renders on state.license changes "
        "— the commit 7be77e6 subscriber was removed. After a "
        "successful license activation, the 'Trial mode' badge "
        "stays stuck until some unrelated event triggers a "
        "re-render (next search keystroke, bookmark toggle, etc.) "
        "The user sees the activation toast + modal flip to Licensed, "
        "then looks at the sidebar still saying Trial, files a "
        "support ticket asking if activation actually worked. "
        "Restore the subscriber OR update this test if the "
        "render dispatch moved into a centralised mapping."
    )


def test_filters_subscriber_persists_sources_on_any_write():
    """Commit a014d76 fixed a real bug: the empty-state "Search
    everything" button in results.js assigned state.sources = ALL
    directly, which updated the in-memory state + re-ran the search
    BUT skipped the localStorage write. After the user clicked the
    button, closed the app, and re-opened, filters.loadSources()
    restored their OLD restricted scope from localStorage — and the
    next search returned zero results AGAIN. They were back where
    they started, exactly the state the "Search everything" button
    promised to fix.

    Fix was a single-line subscriber in filters.initFilters():

      subscribe((k) => { if (k === "sources") _save(); });

    Any state.sources write (popover toggle, popover Reset button,
    results.js empty-state Reset, or any future caller) now persists
    automatically — the localStorage write is no longer the caller's
    responsibility.

    Without a pin, a future refactor that simplifies initFilters()
    could silently drop the subscriber and the bug recurs (the
    explicit _save() calls in _toggle() and the popover Reset still
    work for THOSE paths, so the regression is invisible until a
    user actually clicks "Search everything" in the empty state).
    Pin the subscriber pattern so the bug-fix discipline survives
    future cleanup."""
    src = (REPO_ROOT / "app" / "modules" / "filters.js").read_text(encoding="utf-8")
    assert "subscribe" in src and 'k === "sources"' in src and "_save()" in src, (
        "filters.js no longer has the commit a014d76 subscriber that "
        "persists state.sources writes via ANY path. The explicit "
        "_save() calls in _toggle() and the popover Reset cover "
        "those paths but NOT the empty-state 'Search everything' "
        "reset in results.js, which writes state.sources = ALL "
        "directly. Without the subscriber, a user who clicks "
        "'Search everything' restores wider scope in-memory but "
        "their restricted-scope localStorage value survives the "
        "restart — next search returns 0 results AGAIN, exactly the "
        "state the button promised to fix. Restore the subscriber "
        "pattern or update this test if you've moved persistence "
        "into state.js itself."
    )


def test_results_pill_keeps_bulk_csv_export_button():
    """Commit 4a580cf added an Export CSV button to the bulk-results
    pill alongside Export FCPXML. Closed a real gap: the docs listed
    CSV export, but until commit 4a580cf the only way to actually
    trigger it was via curl.

    Future risk: a refactor that "consolidates" the bulk pill
    buttons could silently drop CSV again, and the docs that name
    the export would point at a button that isn't there.

    Pin the three load-bearing pieces in results.js:
      - the button id (#bulk-export-csv)
      - the visible label ("Export CSV")
      - the _bulkExportCsv handler function

    And in api.js:
      - the exportCsv helper function
    """
    results_src = (REPO_ROOT / "app" / "modules" / "results.js").read_text(encoding="utf-8")
    api_src = (REPO_ROOT / "app" / "modules" / "api.js").read_text(encoding="utf-8")

    assert "bulk-export-csv" in results_src, (
        "results.js no longer renders the #bulk-export-csv button — "
        "the commit 4a580cf Export CSV button on the bulk-results "
        "pill has been removed. The docs still name this export. "
        "Restore the render OR update the docs to match the new UI."
    )
    assert "Export CSV" in results_src, (
        "results.js no longer carries the 'Export CSV' label literal. "
        "If the button got renamed deliberately, update this pin and "
        "any doc that names the label."
    )
    assert "_bulkExportCsv" in results_src, (
        "results.js no longer defines the _bulkExportCsv handler. "
        "The button might still be wired but its click would no-op. "
        "Restore the handler."
    )
    assert "exportCsv" in api_src, (
        "api.js no longer exposes the exportCsv helper — the "
        "bulk-CSV button can't call /api/export/csv without going "
        "back to raw fetch (which skips the commit d252018 timeout / "
        "throw-on-non-2xx / abort-signal plumbing). Restore the "
        "helper."
    )


def test_results_pill_keeps_latency_and_file_count_proof():
    """Commit 078acd2 added "across N files · 0.04s" texture next to
    the result count in the bulk-results pill. The two pieces:

      - latency badge — shows how fast the search actually was at
        the EXACT moment of use ("0.04s" on the first search).
      - file-count fragment — signals real cross-archive search ("12
        results across 4 files"), not regex-on-one-transcript.
        Searching a whole archive at once is the point of the app;
        losing the fragment hides that at the result-list level.

    A future refactor of topbar.js or results.js that silently drops
    either piece tanks both signals. This test pins the contract:
    grep both files for the load-bearing identifiers. If the
    rendering changes shape (e.g., a JSON-stat side panel instead of
    inline pill text), update this test alongside the refactor.
    """
    topbar_src = (REPO_ROOT / "app" / "modules" / "topbar.js").read_text(encoding="utf-8")
    results_src = (REPO_ROOT / "app" / "modules" / "results.js").read_text(encoding="utf-8")

    # topbar.js must measure and store the round-trip ms.
    assert "state.lastSearchMs" in topbar_src, (
        "topbar.js no longer assigns state.lastSearchMs — the commit "
        "078acd2 client-observed latency tracking appears to have been "
        "removed. Without it, the results pill loses its '0.04s' "
        "badge. Restore the "
        "performance.now() round-trip or update this test if the "
        "latency now flows via a different state key."
    )
    assert "performance.now()" in topbar_src, (
        "topbar.js no longer uses performance.now() for the search "
        "round-trip timer — the latency badge may now report stale or "
        "wall-clock-affected timings. Restore the monotonic clock."
    )

    # results.js must render BOTH the latency and the file-count
    # fragments in the bulk pill.
    assert "state.lastSearchMs" in results_src, (
        "results.js no longer reads state.lastSearchMs — the '0.04s' "
        "latency badge on the results pill is gone. Commit 078acd2 "
        "rationale: this is the one user-visible measure of how fast "
        "the search actually was, shown at the moment of use."
    )
    assert "uniqueFiles" in results_src, (
        "results.js no longer computes the unique-file count — the "
        "'across N files' fragment on the results pill is gone. Commit "
        "078acd2 rationale: this signals real cross-archive search "
        "depth, which is the point of searching a whole archive."
    )


def test_topbar_hides_search_count_while_search_is_in_flight():
    """Commit 976a0d3 fixed the stale-count flash: while a new search
    is in flight, state.results still holds the PREVIOUS query's hits
    (the new fetch hasn't returned), so the count badge in the topbar
    showed the OLD number for the ~50-200ms search window. User types
    pricing strategy → sees 14 → types foobar → still sees 14 for a
    blink while the icon pulses. Small but jarring during search-as-
    you-type.

    The fix is one line in the isSearching subscriber — if a new
    search just started, hide the count badge until the results
    subscriber repopulates it. Easy to silently regress: a future
    refactor that splits the subscriber, renames the key, or drops
    the branch (because each piece looks redundant in isolation)
    brings the stale flash back.

    Pin the contract: topbar.js's isSearching subscriber must set
    count.hidden = true when state.isSearching flips true. The
    results subscriber still owns the show-with-new-value path; we
    only assert the hide-during-search path stays in place."""
    topbar_src = (REPO_ROOT / "app" / "modules" / "topbar.js").read_text(encoding="utf-8")

    # The subscriber branch checking isSearching must contain a
    # `count.hidden = true` (or equivalent) line. Loosest viable
    # assertion: the substring `count.hidden = true` appears at least
    # once in the file. If it appears for an unrelated reason in a
    # future refactor (e.g., explicit hide on query-clear), the
    # substring still matches — the pin is permissive of refactors
    # as long as SOMETHING hides the count.
    assert "count.hidden = true" in topbar_src, (
        "topbar.js no longer hides the search-count badge with a "
        "`count.hidden = true` line — the commit 976a0d3 fix that "
        "stops the previous query's hit count from flashing during "
        "the next search has been removed. User retypes a query, "
        "sees stale '14' for the in-flight window, gets confused. "
        "Restore the hide path inside the isSearching subscriber "
        "(or wherever the new render flow lives) and update this "
        "test if the rendering shape genuinely changed."
    )

    # Anchor the hide to the isSearching branch specifically —
    # state.isSearching must still be the trigger. A refactor that
    # moves the hide to a different keypath (e.g., on the abort
    # signal, or inside the fetch call) would silently change the
    # timing and reintroduce a different flavor of the stale flash.
    import re
    isearch_block = re.search(
        r'k\s*===\s*"isSearching"[^}]*?count\.hidden\s*=\s*true',
        topbar_src,
        re.DOTALL,
    )
    assert isearch_block, (
        "topbar.js still has `count.hidden = true` somewhere but "
        "it's no longer inside the isSearching subscriber branch. "
        "The fix relies on the trigger being state.isSearching → "
        "true (the moment a new search starts); moving the hide "
        "elsewhere changes the timing and may reintroduce a "
        "stale-count window. Either keep the hide in the "
        "isSearching branch or update this pin to match the new "
        "trigger keypath."
    )


def test_keyhelp_meta_shortcuts_actually_bound_in_keyboard_js():
    """Every ⌘-modifier shortcut the ⌘/ overlay advertises must
    actually be handled by keyboard.js. Otherwise a user opens the
    shortcut overlay, reads "⌘E export the selected hit as a clip",
    presses ⌘E, and nothing happens — the kind of "this Mac app is
    half-finished" moment that undermines everything else.

    The risk is real: keyboard.js routes meta-modifier chords through
    a flat if/else dispatch with one line per chord. A refactor that
    splits the dispatcher (e.g., moving the export shortcut to a
    detail-pane local listener) can silently break the contract —
    the keyhelp text doesn't know the binding moved. This already
    happened once with ⌘⇧B (the !shift guard was missing on plain
    ⌘B, so the bookmark shortcut was advertised but shadowed by
    sidebar-toggle).

    Pin: parse the SHORTCUTS array in keyhelp.js, extract every chord
    starting with ⌘, and assert keyboard.js contains a matching
    `key.toLowerCase() === "X"` (or `ev.key === ","`) branch. The
    chord-letter extraction is structural — the same data the
    overlay renders to the user.

    Non-meta chords (J/K/L/I/O playback keys, ←/→ lightbox, ⌃click
    context menu, ⌘/ self) are bound in module-local listeners and
    out of scope for this pin — they have their own surface tests
    or are stable-by-isolation."""
    import re

    keyhelp_src = (REPO_ROOT / "app" / "modules" / "keyhelp.js").read_text(encoding="utf-8")
    keyboard_src = (REPO_ROOT / "app" / "modules" / "keyboard.js").read_text(encoding="utf-8")

    # Match the literal entries inside SHORTCUTS — each item is a
    # [chord, description] tuple. We only care about chord strings
    # that start with ⌘ (the global meta-modifier shortcuts routed
    # via keyboard.js's window-level dispatcher).
    chord_pattern = re.compile(r'\["([^"]+)"\s*,\s*"[^"]+"\]')
    all_chords = chord_pattern.findall(keyhelp_src)
    meta_chords = [c for c in all_chords if "⌘" in c]
    assert meta_chords, (
        "keyhelp.js SHORTCUTS parser returned no ⌘-modifier chords — "
        "either keyhelp lost its meta-shortcut section or the regex "
        "drifted. Both cases warrant updating this test."
    )

    # Map each chord to the key letter / punctuation that keyboard.js
    # checks in its dispatch. ⌘/ is bound in keyhelp.js itself
    # (toggleKeyhelp), so skip it.
    # ⌃click is a mouse event, not a keyboard one — skip.
    SKIP = {"⌘ /", "⌃ click"}

    # Pull out the final non-modifier token: "⇧ ⌘ R" → "R", "⌘ ," → ",".
    def _key_token(chord: str) -> str:
        parts = chord.split(" ")
        # Strip empty fragments from the split.
        parts = [p for p in parts if p]
        # The chord's final token is the actual key (everything before
        # is a modifier like ⌘, ⇧, ⌃, ⌥).
        return parts[-1] if parts else chord

    failures: list[str] = []
    for chord in meta_chords:
        if chord in SKIP:
            continue
        key = _key_token(chord)
        # keyboard.js dispatches letter keys via ev.key.toLowerCase()
        # and punctuation via ev.key === "<char>".
        if len(key) == 1 and key.isalpha():
            needle = f'key.toLowerCase() === "{key.lower()}"'
            if needle not in keyboard_src:
                failures.append(
                    f"  {chord!r}: keyboard.js has no '{needle}' branch"
                )
        elif key == ",":
            if 'ev.key === ","' not in keyboard_src:
                failures.append(
                    f"  {chord!r}: keyboard.js no longer matches ev.key === ','"
                )
        else:
            # Unknown chord shape — likely a new symbol added to
            # keyhelp; surface so this test gets updated.
            failures.append(
                f"  {chord!r}: unrecognised final token {key!r} — extend the "
                "test mapping or the chord is malformed in keyhelp.js"
            )

    if failures:
        raise AssertionError(
            "keyhelp.js advertises ⌘-modifier shortcuts that keyboard.js "
            "no longer binds — user opens ⌘/, reads the shortcut, "
            "presses it, nothing happens. Same class of bug as the "
            "earlier ⌘⇧B one (advertised but shadowed by plain ⌘B). Fix by "
            "either restoring the binding in keyboard.js or removing "
            "the entry from keyhelp.js SHORTCUTS so the overlay stops "
            "promising what the app can't deliver.\n\n"
            "Missing bindings:\n" + "\n".join(failures)
        )


def test_sidebar_renders_every_bookmark_not_just_first_six():
    """Commit 72ccd6b removed a `.slice(0, 6)` that capped the sidebar
    bookmark list at 6 rows with no scroll and no 'show all' affordance
    — bookmarks 7-N were rendered nowhere in the DOM, while the count
    badge ("Bookmarks (30)") promised they existed. A power user who
    pinned 10+ moments via ⌘⇧B saw the feature silently broken, and
    a user who finds half the pins gone treats the whole capability
    as half-finished.

    The fix relies on TWO load-bearing pieces:
      1. sidebar.js renders state.bookmarks.map(...) — NOT
         state.bookmarks.slice(0, N).map(...)
      2. sidebar.css has a .sb-sec-scroll rule with max-height +
         overflow-y:auto so the unbounded list scrolls inside the
         section instead of pushing Saved + Recent below the
         viewport (the parent .sidebar is overflow:hidden)

    Either piece silently regressing brings the bug back: a refactor
    that re-adds a slice cap, a CSS purge that drops the .sb-sec-scroll
    rule, or a JS edit that drops the .sb-sec-scroll wrapper. Pin all
    three signals — a future drift in any one of them lights up here."""
    sidebar_src = (REPO_ROOT / "app" / "modules" / "sidebar.js").read_text(encoding="utf-8")
    sidebar_css = (REPO_ROOT / "app" / "sidebar.css").read_text(encoding="utf-8")

    # 1. JS renders the full bookmarks array. Forbid any obvious
    #    "slice the bookmarks list" pattern. A new slice that exists
    #    for an unrelated reason (e.g., taking the FIRST bookmark for
    #    a preview pane) would need to be written so this regex doesn't
    #    misfire — but the current sidebar renders the LIST via
    #    state.bookmarks.map, so any slice on state.bookmarks in
    #    sidebar.js is reasonable to surface for review.
    import re
    slice_pat = re.compile(r"state\.bookmarks\s*\.\s*slice\s*\(")
    if slice_pat.search(sidebar_src):
        raise AssertionError(
            "sidebar.js calls state.bookmarks.slice(...) somewhere — "
            "if this is the bookmark-list render path, it re-caps the "
            "list and hides rows 7-N from the user (commit 72ccd6b "
            "regression). If the slice is for an unrelated reason "
            "(e.g., a preview), refactor so the list render stays "
            "uncapped OR update this pin with the new render shape."
        )

    # 2. JS wraps the bookmark rows in the .sb-sec-scroll container.
    #    Without the wrapper the CSS rule has nothing to attach to and
    #    the section grows unboundedly, pushing Saved + Recent off
    #    the visible viewport (.sidebar is overflow:hidden).
    assert 'class="sb-sec-scroll"' in sidebar_src, (
        "sidebar.js no longer wraps the bookmark rows in a "
        "<div class=\"sb-sec-scroll\"> — the commit 72ccd6b fix that "
        "made the list scroll inside its section has been undone. "
        "Without the wrapper the unbounded list pushes Saved + Recent "
        "below the visible viewport (since the parent .sidebar is "
        "overflow:hidden). Restore the wrapper around the rows in the "
        "sb-bookmarks-sec block."
    )

    # 3. CSS rule for the wrapper still exists. A purge that removes
    #    the .sb-sec-scroll rule silently breaks the scroll behavior:
    #    the wrapper div would render with no max-height, no overflow,
    #    and the list expands unbounded again.
    assert ".sb-sec-scroll" in sidebar_css, (
        "sidebar.css no longer defines a .sb-sec-scroll rule — the "
        "wrapper div in sidebar.js has no styling, so the bookmark "
        "list expands without bound and silently pushes Saved + "
        "Recent off-screen. Restore the rule with max-height + "
        "overflow-y:auto."
    )
    assert "overflow-y" in sidebar_css and "max-height" in sidebar_css, (
        "sidebar.css .sb-sec-scroll rule is missing either max-height "
        "or overflow-y — both are required to make the bookmark "
        "section scroll within itself instead of expanding unbounded."
    )


def test_sidebar_trial_badge_surfaces_activate_cta():
    """Commit 1a8ba53 reframed the sidebar footer badge from a passive
    'Trial mode' status indicator into an action: 'Trial mode · Activate'
    with the '· Activate' fragment styled in the accent color via the
    .sb-license-cta class. Pre-fix, a trial user looking at the always-
    visible badge had no signal that clicking it opens the license
    modal where a key is entered or requested — they had to guess.

    The change is small enough that a future 'tidy up the trial badge
    copy' pass could revert it without realizing the role it plays.
    Pin the three pieces:

      1. sidebar.js renders the 'Activate' word as part of the trial-
         mode label
      2. sidebar.js wraps it in the .sb-license-cta span so the accent
         styling actually applies
      3. sidebar.css defines .sb-license-cta with a color rule (the
         class must be styled or the cue disappears)

    Licensed users see the plain 'Licensed' badge — no CTA needed
    once a key is active. This test pins only the trial-state
    contract; the licensed-state branch stays whatever sidebar.js
    decides."""
    sidebar_src = (REPO_ROOT / "app" / "modules" / "sidebar.js").read_text(encoding="utf-8")
    sidebar_css = (REPO_ROOT / "app" / "sidebar.css").read_text(encoding="utf-8")

    # 1 + 2: JS surfaces the Activate word AND wraps it in the cta span.
    # Single combined string assertion catches both at once: if either
    # the word OR the class disappears, the regex fails.
    assert "sb-license-cta" in sidebar_src, (
        "sidebar.js no longer references the .sb-license-cta span — "
        "the commit 1a8ba53 trial badge 'Activate' hint has been "
        "removed or refactored away. A trial user looking at the "
        "always-visible badge again has no visible affordance that "
        "clicking opens the license modal. "
        "Restore the span OR update this pin if the trial-badge "
        "copy was deliberately redesigned (and make sure the "
        "replacement still cues the click affordance)."
    )
    assert "Activate" in sidebar_src, (
        "sidebar.js no longer contains the word 'Activate' anywhere "
        "— the trial badge action label is gone. Same discoverability "
        "failure mode as removing the .sb-license-cta "
        "span. Restore the label or update this pin if the trial "
        "badge copy intentionally changed."
    )

    # 3: CSS rule for the cta class still styles it. Without the rule,
    # the span renders in plain text color — visible but no longer
    # acts as a visual affordance.
    assert ".sb-license-cta" in sidebar_css, (
        "sidebar.css no longer defines a .sb-license-cta rule — the "
        "trial badge 'Activate' hint renders in the same color as "
        "the surrounding 'Trial mode' text, so the visual cue that "
        "drew the eye to the action disappears. Restore the rule "
        "with a color: var(--accent) (or equivalent) to keep the "
        "affordance visible."
    )


def test_prefs_advanced_keeps_license_button():
    """A License… button in the Preferences → Advanced section mirrors
    how most macOS apps surface activation in their preferences pane.
    Before it existed Preferences had no License entry — and the docs
    told users to look there anyway, sending them on a wild goose
    chase (commits 7fbd415 + a758115 fixed both copy mistakes by
    pointing at the sidebar badge instead).

    Now the path is real. Three discoverable entry points to the
    license modal:
      - Sidebar 'Trial mode · Activate' badge (always visible)
      - ⌘/ keyhelp overlay → 'License…' link
      - ⌘, Preferences → Advanced → 'License…' (this entry)

    Pin the third entry against silent removal: a future change that
    tidies the Advanced section might drop the License button as
    'redundant', which silently re-creates the bug class commit
    7fbd415 fixed (a doc instruction that matches no UI). Two
    structural assertions:

      1. prefs.js renders an id="prefs-license" button (the actual
         DOM hook the click handler binds to)
      2. prefs.js click-handler for that button dispatches the
         tern:open-license-modal CustomEvent (the single event the
         license modal listens for)

    An earlier pin forbade any doc from mentioning a Preferences →
    License path, because none existed. Now that the path is real,
    the inverse contract (assert the menu entry exists) keeps the
    same regression-class protection from a different angle."""
    prefs_src = (REPO_ROOT / "app" / "modules" / "prefs.js").read_text(encoding="utf-8")

    # 1. The button must exist in the rendered template. The id is
    # what the click handler grabs via querySelector — a refactor
    # that renames the id would also need to update the handler
    # selector, so locking the id pins both ends.
    assert 'id="prefs-license"' in prefs_src, (
        "prefs.js no longer renders a button with id=\"prefs-license\" "
        "— the Preferences → Advanced → License… entry has been "
        "removed. Users expecting the macOS-standard activation path "
        "get a dead end again. Restore the button OR update any doc "
        "that references this path alongside the removal."
    )

    # 2. The click handler still dispatches the cross-module event
    # the license modal listens for. A refactor that drops the
    # dispatch silently turns the button into a no-op.
    assert "prefs-license" in prefs_src and "tern:open-license-modal" in prefs_src, (
        "prefs.js still has the License button but no longer dispatches "
        "tern:open-license-modal — clicking the button does nothing. "
        "Restore the dispatch in the #prefs-license click handler."
    )


def test_empty_hero_doesnt_render_hardcoded_query_pills_on_empty_workspace():
    """Commit 9f86edb removed the DEMO_QUERIES array from empty.js. The
    array used to seed the day-1 empty-workspace hero with six clickable
    pills (pricing strategy, Series A funding, first hire, orange cat,
    mountain lake, rabbit in a forest). Clicking ANY of them on a
    workspace with 0 indexed files fired a search that returned 0 hits
    — because there was nothing to search against. The user's only
    productive path collapsed to: click pill → see 0 results → read
    the index-a-folder tip → click the actual CTA.

    The fix made the empty-workspace hero render pills ONLY when
    state.recents is non-empty (recents pills retain semantic — a real
    re-engagement signal). When recents are empty too, no pills render,
    and every element in the hero points at the single productive
    action (the Index your folder CTA).

    Pin the contract so a future change doesn't re-add a hardcoded
    DEMO_QUERIES array (or any equivalent that seeds clickable pills
    with non-recent data on a 0-files workspace). Two structural
    assertions:

      1. No const declaration for DEMO_QUERIES (or any name suggesting
         a hardcoded clickable-query array) on the empty hero path
      2. The pills source is gated on hasRecents — when recents are
         empty, the pills array is empty

    A redesign that adds an empty-workspace tutorial OR a different
    affordance is fine; this pin only catches the specific 'render
    pills that always 0-result' regression."""
    empty_src = (REPO_ROOT / "app" / "modules" / "empty.js").read_text(encoding="utf-8")

    # 1. The const declaration must be absent (the WORD can still
    # appear in the explanatory comment block — only `const DEMO_QUERIES
    # = [` matches the array literal that re-introduces the bug).
    import re
    if re.search(r"\bconst\s+DEMO_QUERIES\s*=\s*\[", empty_src):
        raise AssertionError(
            "empty.js declares a const DEMO_QUERIES array — the commit "
            "9f86edb removal has been undone. If left alone the hero "
            "renders the array as clickable pills, every one of which "
            "guarantees a 0-results dead-end on a workspace with no "
            "indexed files. If you intentionally need a hardcoded "
            "query list, gate the rendering on workspace-non-empty "
            "(state.stats.files_total > 0) so a click can actually "
            "return hits — and update this test."
        )

    # 2. The pills array must be empty when there are no recents.
    # The commit 9f86edb pattern is `hasRecents ? state.recents.slice(...) : []`.
    # An obvious regression is `: SOMETHING_HARDCODED` instead of `: []`.
    # Scan for the ternary's else-branch and require it to be a literal
    # empty array.
    pills_pattern = re.compile(
        r"hasRecents\s*\?\s*state\.recents\.[^:]+:\s*(\[\s*\]|\[\])",
        re.MULTILINE,
    )
    if not pills_pattern.search(empty_src):
        raise AssertionError(
            "empty.js no longer gates the hero pills on `hasRecents "
            "? recents : []` (or the pattern was refactored). The pin "
            "expected the empty-workspace branch to render an empty "
            "pills array. If the rendering shape changed, update this "
            "test alongside the change — and make sure the new shape "
            "still avoids seeding clickable pills with content that "
            "guarantees a 0-results click on an empty workspace."
        )


def test_indexing_progress_counts_skipped_files_toward_processed():
    """Commit 8d94687 fixed the progress bar stalling under 100% when
    re-indexing a folder where most files were already up-to-date.
    Backend now tracks files_skipped alongside files_done /
    files_errored; the frontend rolls them into a `processed` count
    so the bar reaches 100%, the in-progress title matches the bar,
    the done toast surfaces 'N already up-to-date', and ETA stops
    over-estimating on mixed batches.

    Two load-bearing pieces back this:
      1. api/main.py initializes files_skipped: 0 in BOTH the dormant
         state init AND the per-run init at /api/index/start. Skip the
         per-run init and the field is absent on the very first poll
         after a fresh run started.
      2. api/main.py increments app.state.indexing['files_skipped']
         in the else branch of `if indexed: …` — the branch that fires
         when indexer.index_file returns False (already up-to-date).
         Skip the increment and the counter never moves; bar stalls
         again.
      3. app/modules/indexing.js reads s.files_skipped and includes
         it in `processed = done + skipped + errored`. Drop the
         reference and the bar / title / ETA revert to using only
         `done`, re-introducing the stall.

    A refactor of the indexing loop that doesn't touch the explicit
    increment (e.g., switching to a callback-based progress reporter
    that emits only 'indexed'/'errored' events) is the realistic
    regression vector. Pin all three signals so the bug can't sneak
    back in via a one-line drop."""
    api_src = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    indexing_js = (REPO_ROOT / "app" / "modules" / "indexing.js").read_text(encoding="utf-8")

    # Backend init: files_skipped MUST appear at least twice — once in
    # the dormant-state init block (so /api/index/status returns a stable
    # shape before any run starts) and once in the per-run init at
    # /api/index/start (so each fresh run resets the counter to 0).
    assert api_src.count('"files_skipped"') >= 2, (
        "api/main.py no longer initializes files_skipped in both the "
        "dormant state AND the per-run /api/index/start block — at "
        "least one of the two init points is missing. Without both, "
        "the frontend's `s.files_skipped || 0` fallback hides the "
        "regression at first (returns 0) until a re-indexing run "
        "actually starts incrementing a non-existent key, which "
        "silently no-ops. Bar stalls, ETA over-estimates, done toast "
        "under-counts — the exact bug commit 8d94687 fixed."
    )

    # Backend increment: the else branch of the indexing loop must
    # bump files_skipped. The exact `app.state.indexing["files_skipped"]
    # += 1` pattern is the load-bearing line.
    assert 'app.state.indexing["files_skipped"] += 1' in api_src, (
        "api/main.py no longer increments files_skipped in the "
        "indexing loop's 'already up-to-date' branch (the else of "
        "`if indexed:`). The counter stays at 0 for the whole run, "
        "so the frontend's processed = done + skipped + errored "
        "still equals done — bar back to stalling under 100% on any "
        "re-index of a partially-done folder. Restore the increment "
        "or update this test if the indexing-loop shape changed "
        "(and make sure the new shape still surfaces the skipped "
        "count somewhere the frontend reads)."
    )

    # Frontend: the processed computation MUST reference files_skipped.
    # Without it, only files_done counts and the bar stalls again.
    assert "files_skipped" in indexing_js, (
        "app/modules/indexing.js no longer references s.files_skipped "
        "— the commit 8d94687 fix that made the bar reach 100% on "
        "mixed batches has been undone. The frontend reverted to "
        "computing pct from files_done alone, so re-indexing a "
        "folder where most files were already done stalls the bar "
        "under 100% with no explanation in the toast."
    )
    # Pin the actual formula shape so a refactor that renames
    # `processed` doesn't silently drop the skipped contribution.
    # The key invariant: done + skipped + errored appears together
    # in some form. The exact local variable name is fungible; the
    # COMBINATION is what catches the regression.
    assert ("done + skipped" in indexing_js) or ("skipped + done" in indexing_js), (
        "app/modules/indexing.js no longer combines files_done with "
        "files_skipped in any obvious additive expression — the bar "
        "calc may have been refactored in a way that dropped one of "
        "the two. Restore the combined progress count or update this "
        "test if the rendering shape genuinely changed."
    )


def test_indexing_done_toast_doesnt_claim_success_when_all_files_failed():
    """Commit f2ff5f2 fixed a trust-killer in the done toast: when
    every file in a batch failed (done === 0 && errs > 0), the
    sub-text still cheerfully said 'Search updated with the new
    files.' A user dragged a folder, indexer rejected every file
    (codec, path, permission), toast claimed success, the user assumed
    Tern was broken instead of looking at the log.

    The fix branches the sub-text on the success/failure mix:
      - all-failed: points at ~/Library/Logs/tern-crash.log
      - mostly-failed (errs > done && errs >= 3): same log pointer
      - some-succeeded: unchanged 'Search updated…'

    Easy to silently regress: a future refactor that 'simplifies'
    the done-toast template back to a single hardcoded string would
    re-create the dishonest-success branch. Pin the two failure-
    case strings + the log-file reference so the regression lights
    up here instead of via a bug report."""
    indexing_js = (REPO_ROOT / "app" / "modules" / "indexing.js").read_text(encoding="utf-8")

    # All-failed branch must still exist (case 1: done === 0 && errs > 0).
    # The exact phrase 'failed to index' is the load-bearing surface;
    # a refactor that keeps the conditional but drops the wording
    # silently re-introduces the dishonest 'updated with the new files'
    # default.
    assert "failed to index" in indexing_js, (
        "app/modules/indexing.js no longer contains 'failed to index' "
        "anywhere — the commit f2ff5f2 all-files-failed sub-text branch "
        "is gone. The done toast will silently revert to 'Search "
        "updated with the new files' even when nothing actually "
        "indexed. Restore the conditional in _renderDoneToast or "
        "update this pin if the rendering shape genuinely changed."
    )

    # The log-file pointer is the actionable piece: tells the user
    # where to look for the per-file traceback log_event wrote.
    # Without it, the user is just told 'failed' with no path forward.
    assert "tern-crash.log" in indexing_js, (
        "app/modules/indexing.js no longer references tern-crash.log "
        "in the done-toast sub-text — the actionable log pointer the "
        "commit f2ff5f2 fix added has been removed. The user sees "
        "'failed' without a path to the per-file errors. Restore "
        "the pointer (the indexer writes per-file errors via "
        "log_event('index_file_failed', ...) — commit 8377671)."
    )

    # The conditional that branches on outcome must still exist.
    # `done === 0` (or `done == 0`, depending on style) gating the
    # all-failed branch is the load-bearing test — without it, the
    # branch fires for the wrong inputs (or never fires).
    import re
    gate_pattern = re.compile(r"done\s*===?\s*0\s*&&\s*errs?\s*>\s*0")
    assert gate_pattern.search(indexing_js), (
        "app/modules/indexing.js no longer gates the all-failed "
        "branch on `done === 0 && errs > 0` (or the equivalent). "
        "Without the gate the conditional doesn't differentiate "
        "all-failed from some-succeeded, and the honest-outcome signal "
        "the fix added is gone. Restore the gate or update this pin."
    )


def test_empty_state_surfaces_files_errored_when_nonzero():
    """Commit 4e3dfa3 added files_errored to /api/stats and commit
    7feba3b wired it into the empty-state library header. The
    persistent error count (status='error' rows in the files table,
    accumulating across runs) is now visible as a "⚠ N files failed
    to index" line below the kind breakdown when errored > 0.

    Three load-bearing pieces:
      1. storage.stats() returns the files_errored field (commit
         4e3dfa3 SQL aggregate)
      2. empty.js reads s.files_errored
      3. empty.js renders the warning line when erroredCount > 0

    A future cleanup that "simplifies" the empty-state header back
    to just the kind breakdown silently re-hides the persistent
    error count. The user can keep accumulating indexing failures
    across runs with no in-app signal.

    Pin all three structural pieces so the regression class lights
    up here instead of via a user-reported 'I indexed a folder of
    100 files but only 73 show up in search'."""
    storage_src = (REPO_ROOT / "service_pipeline" / "tern" / "storage.py").read_text(encoding="utf-8")
    empty_js = (REPO_ROOT / "app" / "modules" / "empty.js").read_text(encoding="utf-8")

    # 1. Backend: stats() must compute files_errored. The SQL aggregate
    # uses CASE WHEN status='error' — pin both ends.
    assert "files_errored" in storage_src, (
        "service_pipeline/tern/storage.py no longer references "
        "files_errored — the SQL aggregate that commit 4e3dfa3 added "
        "for /api/stats has been removed. The empty-state warning "
        "line + qa_smoke stats pin both depend on this field; "
        "restore the SQL CASE clause or update this test."
    )

    # 2 + 3. Frontend: empty.js reads s.files_errored AND renders the
    # warning line. Combined string assertion catches removal of
    # either piece.
    assert "files_errored" in empty_js, (
        "app/modules/empty.js no longer references s.files_errored "
        "— the commit 7feba3b empty-state warning line that surfaces "
        "the persistent indexing-error count has been removed. The "
        "user accumulates indexing failures across runs with no "
        "in-app signal. Restore the conditional rendering in "
        "_renderList or update this test if the rendering shape "
        "genuinely changed."
    )
    assert "failed to index" in empty_js, (
        "app/modules/empty.js no longer contains the literal "
        "'failed to index' string — the warning-line copy has been "
        "removed. Restore it (or update the wording AND this pin)."
    )


def test_sidecar_shutdown_contract_in_rust_shell():
    """Pin for the three-layer sidecar lifecycle in
    tauri/src-tauri/src/main.rs. The real-world failure this guards:
    a user reported 15 GB RAM held by THREE ~5 GB orphan python
    sidecars that survived for days after closing Tern.

    Two root causes were found and fixed:
      1. WindowEvent::CloseRequested was the ONLY kill path — and it
         does NOT fire on Cmd-Q / AppleScript quit / Dock quit
         (verified live). EVERY normal quit leaked one sidecar.
         Fix: kill_sidecar() called from RunEvent::Exit too.
      2. Force-quit / OOM / crash skip all userspace cleanup.
         Fix: PID-file orphan-killer at startup (kill_orphan_sidecar)
         with a ps-based uvicorn+main:app identity check against PID
         recycling, SIGTERM → 2s wait → SIGKILL escalation.

    A refactor that drops any piece silently re-opens the leak. Pin
    the load-bearing source patterns:"""
    main_rs = (REPO_ROOT / "tauri" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")

    assert "RunEvent::Exit" in main_rs, (
        "main.rs no longer handles RunEvent::Exit — Cmd-Q (the most "
        "common way to quit a Mac app) stops killing the sidecar and "
        "every normal quit leaks a ~5 GB python again. Restore the "
        ".build().run(|app_handle, event| ...) exit hook."
    )
    assert "fn kill_sidecar" in main_rs, (
        "main.rs no longer defines kill_sidecar — the shared, "
        "idempotent kill helper both shutdown hooks call."
    )
    assert "fn kill_orphan_sidecar" in main_rs and "kill_orphan_sidecar();" in main_rs, (
        "main.rs no longer defines/calls kill_orphan_sidecar at "
        "startup — force-quit / OOM / crash orphans accumulate "
        "forever again (the original 3×5 GB report)."
    )
    assert "sidecar.pid" in main_rs, (
        "main.rs no longer references the sidecar.pid file — the "
        "orphan-killer has nothing to read and the contract is dead."
    )
    assert "uvicorn" in main_rs and "main:app" in main_rs, (
        "main.rs no longer identity-checks the orphan PID against "
        "the Tern uvicorn command line — PID recycling could kill "
        "an unrelated user process. Restore the ps -p check."
    )
    assert "process_group(0)" in main_rs, (
        "main.rs no longer puts the sidecar in its own process group "
        "— group-SIGTERM can't take down whisper/ffmpeg children."
    )


def test_saved_searches_snapshot_scope_for_restoration():
    """Saved searches pin a query AND its scope — re-running one
    months later returns the same hits even if the Speech/On-screen/
    Visual toggles changed in the meantime. The feature is only
    worth having if that contract holds.

    The contract has three load-bearing pieces:
      1. saved.js toggle() captures scope.sources AND scope.folder
         in the persisted entry (without these fields, the entry
         is just {query, hits, ts} — running it re-uses whatever
         the current global scope is, which is exactly the
         no-snapshot fallback)
      2. saved.js getSavedScope() returns the captured scope
         (without this getter, the sidebar click-handler has no
         way to read the snapshot back)
      3. sidebar.js click-handler reads getSavedScope and applies
         the snapshot to state.sources + state.folderFilter
         (without this, the entry's scope sits in localStorage
         but never reaches the search engine)

    All three pieces ship today. A refactor that drops any one
    silently breaks the contract. Pin each piece structurally so the
    regression lights up here instead of via a user-reported 'my
    saved search Speech-only-folder=clients returns the wrong hits
    now'."""
    saved_js = (REPO_ROOT / "app" / "modules" / "saved.js").read_text(encoding="utf-8")
    sidebar_js = (REPO_ROOT / "app" / "modules" / "sidebar.js").read_text(encoding="utf-8")

    # 1. saved.js toggle() must capture scope into the entry. The
    # current shape stores scope.sources + scope.folder as
    # OPTIONAL fields (legacy entries without them still work).
    # Lock both field names against silent removal.
    assert "entry.sources" in saved_js, (
        "saved.js no longer assigns entry.sources — the scope-"
        "snapshot contract has been broken. New saved entries lose "
        "the per-source filter snapshot; re-running them months "
        "later uses the current global scope instead of the saved "
        "one. Restore the assignment in toggle() or update this pin."
    )
    assert "entry.folder" in saved_js, (
        "saved.js no longer assigns entry.folder — the folder-"
        "filter snapshot is gone. Same trust-kill: re-running a "
        "saved search restores the query but not the folder it "
        "was saved with."
    )

    # 2. The getSavedScope getter must still exist and return the
    # snapshot. Without it, the sidebar click-handler can't read
    # the persisted scope back.
    assert "function getSavedScope" in saved_js or "export function getSavedScope" in saved_js, (
        "saved.js no longer exports getSavedScope — the sidebar "
        "click-handler that applies the saved-search scope can't "
        "read the snapshot back, so re-running a saved entry "
        "silently uses the current global scope. Restore the "
        "getter or update the pin if the API genuinely changed."
    )

    # 3. sidebar.js must consume getSavedScope when a saved row is
    # clicked. Without this, the snapshot is stored AND readable but
    # never actually applied to the in-flight search.
    assert "getSavedScope" in sidebar_js, (
        "app/modules/sidebar.js no longer calls getSavedScope — "
        "saved searches still capture + persist their scope but "
        "the click-handler never reads it back, so the search "
        "runs under whatever the current global scope is. The "
        "user-visible failure mode is the 'why does my saved search "
        "return DIFFERENT hits today?' bug report."
    )


def test_trim_widget_recent_features_load_bearing_pieces():
    """Commits 8ca914d → a89f794 shipped five trim-widget improvements
    in a row, each closing a real user-visible gap. The README
    describes the trim editor (dual-zoom timeline, keyframe snapping,
    frame stepping), so a regression in any of these silently makes
    that description false.

    Pin the load-bearing pieces across player.js + player.css so a
    future refactor that "simplifies" the trim widget can't drop any
    of them silently:

      8ca914d  band clamp + chevron + trackpad pan + overview wheel
      528e5d6  floating duration badge inside the band
      34c48aa  right-click context menu (Reset / Fit / Zoom / Copy)
      3539886  inline IN/OUT validation toast + shake pulse
      a89f794  snap-to-keyframe-boundary

    Each assertion below catches a different regression class. The
    string-match approach is intentionally permissive — a refactor
    that renames an internal helper still passes as long as the
    user-visible CONTRACT (the rendered DOM hook, the CSS class
    name, the event-handler keyword) survives."""
    player_js  = (REPO_ROOT / "app" / "modules" / "player.js").read_text(encoding="utf-8")
    player_css = (REPO_ROOT / "app" / "player.css").read_text(encoding="utf-8")

    # 8ca914d: band clamp via Math.max/min into [0%, 100%]
    assert "bandLeftPct" in player_js and "bandRightPct" in player_js, (
        "player.js no longer clamps the band to bandLeftPct / "
        "bandRightPct — when the clip extends past the viewport, "
        "the band will bleed off the right edge via parent overflow:"
        "hidden, looking like the trim widget is broken. Restore "
        "the Math.max(0, wStartPct) / Math.min(100, wEndPct) clamp "
        "in _layoutAndPublish."
    )

    # 8ca914d: chevron CSS at the clamped edge
    assert 'data-clipped-left="1"' in player_css and 'data-clipped-right="1"' in player_css, (
        "player.css no longer styles the clipped-edge chevron — the "
        "user loses the visual cue that the selection continues past "
        "the viewport edge."
    )

    # 8ca914d: trackpad pan shared helper + the overview wheel listener
    assert "_panViewByPx" in player_js, (
        "player.js no longer defines _panViewByPx — the shared "
        "trackpad-pan helper used by both the working-strip and "
        "overview wheel listeners has been removed. Two-finger "
        "horizontal swipe stops panning the view."
    )
    assert 'overview.addEventListener("wheel"' in player_js, (
        "player.js no longer has a wheel handler on the overview — "
        "trackpad swipe over the minimap stops working as a pan "
        "shortcut (forces the user back to dragging the viewport "
        "rectangle for the same gesture)."
    )

    # 528e5d6: floating duration badge in the band
    assert "vtrim-work-band-dur" in player_js and "vtrim-work-band-dur" in player_css, (
        "Floating duration badge (commit 528e5d6) missing from either "
        "player.js (the DOM hook) or player.css (the pill styling). "
        "Final Cut / Premiere / CapCut convention is to show clip "
        "duration centered on the timeline rectangle — losing it "
        "downgrades the trim widget against what the README describes."
    )

    # 34c48aa: right-click context menu
    assert 'work.addEventListener("contextmenu"' in player_js, (
        "player.js no longer wires the right-click context menu on "
        "the working strip — Reset / Fit / Zoom / Copy actions stop "
        "being reachable via the universal NLE right-click muscle "
        "memory (Final Cut / Premiere / DaVinci / CapCut)."
    )

    # 3539886: input validation toast + invalid-pulse class
    assert "vtrim-input-invalid" in player_js and "vtrim-input-invalid" in player_css, (
        "Inline IN/OUT validation (commit 3539886) missing — typed "
        "unparseable timecodes silently revert again instead of "
        "toasting + shake-pulsing the offending input."
    )

    # a89f794: snap-to-keyframe candidates
    assert '"keyframe"' in player_js or "label: 'keyframe'" in player_js, (
        "player.js _maybeSnap no longer pushes a 'keyframe' candidate "
        "— handles stop snapping to the actual thumbnail-edge ts_ms "
        "values, so cuts land mid-GOP and ffmpeg has to re-encode "
        "the partial frame on export. Restore the _thumbsAll loop "
        "in the candidate-build block."
    )

    # cb69cd4: zoom-level indicator in the info row
    assert "vtrim-zoom-level" in player_js and "vtrim-zoom-level" in player_css, (
        "Zoom-level indicator (commit cb69cd4) missing from either "
        "player.js (the DOM hook) or player.css (the muted-mono "
        "styling). Without it a scroll-zoom past the 4s floor reads "
        "as 'the widget is broken' instead of 'youre zoomed in too "
        "far — click ⤡ or scroll to zoom out'."
    )

    # 62af077: native hover tooltip on the working strip
    assert "work.title" in player_js, (
        "player.js no longer assigns work.title in _layoutAndPublish "
        "— the hover tooltip that surfaces full clip range + off-"
        "screen overflow extent has been removed. Users lose the "
        "explanation for the chevron stripe when the clip extends "
        "past the visible viewport."
    )

    # 6bc43a2: snap color-coding via data-snap attribute
    assert "snapFlash.dataset.snap" in player_js, (
        "player.js no longer sets snapFlash.dataset.snap — the "
        "color-coded snap-flash variants (commit 6bc43a2) are no "
        "longer driven by the data-snap attr, so all snaps re-flash "
        "in the same accent color and users lose the visual "
        "differentiation between playhead / match / keyframe snaps."
    )
    assert 'data-snap="match"' in player_css and 'data-snap="keyframe"' in player_css, (
        "player.css no longer styles the snap-flash variants for "
        "match / keyframe targets — the visual color-coding is "
        "gone even if the JS still sets data-snap. Restore the CSS "
        "selectors or expect every snap to flash blue."
    )

    # 1ed1f02: Shift-bypass snap during handle drag
    assert "lastShift" in player_js, (
        "player.js no longer tracks lastShift in the handle-drag "
        "closure — the standard-NLE Shift-bypass-snap escape hatch "
        "is gone. Users cant set IN/OUT precisely past a snap-zone "
        "boundary without manually typing the timecode. Restore "
        "the let lastShift + the _move update + the lastShift ? "
        "null : _maybeSnap gate."
    )
    assert "bypass snap" in player_js, (
        "The trim widget hint row no longer mentions 'bypass snap' "
        "— users have no way to discover the Shift-modifier "
        "gesture without reading the source. Restore the hint copy "
        "(or update both the hint AND this pin if the wording "
        "deliberately changed)."
    )


def test_readme_try_searching_parser_self_check():
    """Parser correctly extracts backticked queries from a synthetic
    README. Pinned so a parser refactor can't silently make the main
    test vacuously pass (empty queries → no failures → false-green)."""
    src = (
        "# Tern\n"
        "\n"
        "Try searching:\n"
        "- `first query` (description)\n"
        "- `second` / `third` / `fourth` (multi)\n"
        "- `last query`\n"
        "\n"
        "## Other section\n"
        "- `should not be picked up`\n"
    )
    out = _parse_readme_try_searching_queries(src)
    assert out == ["first query", "second", "third", "fourth", "last query"], out

    # Missing section — return empty list (the main test then fails
    # loud via its own assertion that queries is non-empty).
    assert _parse_readme_try_searching_queries("# no try searching here") == []


def test_placeholder_parser_self_check():
    """The _parse_placeholder_examples helper itself works correctly.
    Pin against synthetic input so a future refactor of the parser
    can't silently break the invariant by failing to find any examples
    (which would make the main test vacuously pass)."""
    src = '''
        const PLACEHOLDER_EXAMPLES = [
          "Find any moment…",
          "pricing strategy",
          "Stanford",
        ];
    '''
    out = _parse_placeholder_examples(src)
    assert out == ["Find any moment…", "pricing strategy", "Stanford"], out

    # Embedded escaped quote — confirms unicode_escape decode path.
    src2 = 'const PLACEHOLDER_EXAMPLES = ["he said \\"hi\\""];'
    out2 = _parse_placeholder_examples(src2)
    assert out2 == ['he said "hi"'], out2

    # Missing array — parser must raise loud, not return empty (empty
    # would make the main test vacuously pass).
    with pytest.raises(AssertionError, match="couldn't locate"):
        _parse_placeholder_examples("// no array here")
