"""Build-time invariant: NO nested backticks inside any frontend
`<varname>.innerHTML = ` ... ` ` template literal.

WHY THIS TEST EXISTS
====================

A CRITICAL failure mode: an explanatory HTML comment that includes a
code snippet wrapped in backticks:

    <!-- ... via `video.currentTime = nextT` in the drag handler ... -->

That comment lives INSIDE a JS template literal:

    container.innerHTML = `
      ...
      <!-- ... via `video.currentTime = nextT` in the drag handler ... -->
      ...
    `;

The JS parser doesn't care that the backtick is "inside an HTML
comment" — HTML comments are not a JS construct. The first backtick
CLOSED the template literal early, turning subsequent text into raw
JavaScript. WKWebView's JavaScriptCore crashed with:

    Unexpected identifier 'video' — try reloading

User-visible blast radius: the ENTIRE video detail pane failed to
render. Title bar showed the error toast; below it, only a stray
.vtrim-work-band escaped to viewport-fill on the left (its parent
.vtrim-work never rendered because the template literal aborted
partway through).

Bizarrely, `node --check` ACCEPTED the file. The two parsers
(V8/Node vs JavaScriptCore/WebKit) handle template-literal-nesting
ambiguity differently — Node's was permissive enough to let the
broken code through pre-commit syntax checks.

This test runs at pytest time and fails LOUDLY if any future edit
re-introduces a nested backtick inside any innerHTML template.
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
APP_MODULES = REPO_ROOT / "app" / "modules"


def _find_innerhtml_template_ranges(source: str) -> list[tuple[int, int]]:
    """Return list of (start_idx, end_idx) char-index pairs for each
    `<anything>.innerHTML = \\`` template literal in `source`.

    start_idx points at the opening backtick; end_idx points at the
    matching closing backtick (the one followed by `;`).

    HEURISTIC for the close:
    A pure JS parser walking backtick-by-backtick can't tell the
    difference between "the genuine closing backtick" and "the
    accidentally-nested backtick I shouldn't be here" — both look
    like backticks. By the time the parser sees a malformed nested
    backtick it has already locked in that as the close.

    For the codebase's actual `.innerHTML = \\`...\\`;` assignments,
    the close ALWAYS appears as backtick-then-semicolon (possibly
    with surrounding whitespace). So: walk forward and treat as the
    close ONLY the first unescaped non-interpolated backtick that is
    immediately followed by `;` (optionally preceded by whitespace
    on the same line). Backticks earlier than that — including the
    accidentally-nested ones — get reported as nested-bad by the
    nested-scan in `_nested_backticks_in_template`.

    Skips `\\\\\\`` escapes and `${...}` interpolation blocks
    correctly (sub-templates inside interpolation are legitimate
    JS and not the bug we're hunting).
    """
    ranges: list[tuple[int, int]] = []
    i = 0
    n = len(source)
    while i < n:
        m = source.find(".innerHTML = `", i)
        if m < 0:
            break
        open_idx = m + len(".innerHTML = `") - 1  # index of the `
        # Walk forward, scoring each candidate closing backtick by
        # whether it's followed by `;`. Skip escapes + interpolation.
        j = open_idx + 1
        close_idx = -1
        while j < n:
            c = source[j]
            if c == "\\":
                j += 2; continue
            if c == "$" and j + 1 < n and source[j + 1] == "{":
                depth = 1
                j += 2
                while j < n and depth > 0:
                    if source[j] == "\\":
                        j += 2; continue
                    if source[j] == "{":
                        depth += 1
                    elif source[j] == "}":
                        depth -= 1
                    j += 1
                continue
            if c == "`":
                # Is this followed by `;` (optionally after whitespace
                # on the same line)? Then it's the real close.
                k = j + 1
                while k < n and source[k] in " \t":
                    k += 1
                if k < n and source[k] == ";":
                    close_idx = j
                    break
                # Otherwise it's a nested backtick (the bug we're
                # hunting). Keep walking — the genuine close is later.
            j += 1
        if close_idx < 0:
            # Ran off end without a `;-terminated backtick — degenerate
            close_idx = n - 1
        ranges.append((open_idx, close_idx))
        i = close_idx + 1
    return ranges


def _nested_backticks_in_template(source: str, start: int, end: int) -> list[int]:
    """Return char-indices of any disallowed nested backticks inside
    the template body source[start+1 : end] (exclusive of bookend
    backticks). Skips backticks that live inside `${...}`
    interpolation blocks AND inside `\\\\\\`` escape sequences."""
    bad: list[int] = []
    j = start + 1
    while j < end:
        c = source[j]
        if c == "\\":
            j += 2; continue
        if c == "$" and j + 1 < end and source[j + 1] == "{":
            # Skip interpolation block (matching braces)
            depth = 1
            j += 2
            while j < end and depth > 0:
                if source[j] == "\\":
                    j += 2; continue
                if source[j] == "{":
                    depth += 1
                elif source[j] == "}":
                    depth -= 1
                j += 1
            continue
        if c == "`":
            bad.append(j)
        j += 1
    return bad


def _line_of(source: str, idx: int) -> int:
    """1-indexed line number of source[idx]."""
    return source.count("\n", 0, idx) + 1


def _gather_module_files() -> list[Path]:
    """All .js files under app/modules/, excluding iCloud-dupe ` 2.js`
    siblings (Finder's iCloud Drive duplicate-naming convention —
    they're not shipped; prepare_bundle.sh excludes them from rsync
    and .gitignore excludes them from git tracking; tests should
    ignore them too)."""
    if not APP_MODULES.is_dir():
        pytest.skip(f"frontend module dir not present: {APP_MODULES}")
    return sorted(
        p for p in APP_MODULES.glob("*.js")
        if " 2.js" not in p.name and not p.name.endswith(" 2.js")
    )


def test_no_nested_backticks_in_innerhtml_templates():
    """For every .innerHTML = `...` template literal in every
    app/modules/*.js, the body must contain ZERO unescaped, non-
    interpolated backticks.

    A nested backtick closes the outer template prematurely. Node's
    parser accepts the result (silently mangled string); WebKit's
    JavaScriptCore rejects it with "Unexpected identifier" at runtime,
    breaking whatever pane renders that template. The whole pane
    fails to render when this happens.
    """
    failures: list[str] = []
    for module in _gather_module_files():
        source = module.read_text(encoding="utf-8")
        for (start, end) in _find_innerhtml_template_ranges(source):
            for bad_idx in _nested_backticks_in_template(source, start, end):
                template_line = _line_of(source, start)
                bad_line = _line_of(source, bad_idx)
                # Extract the offending line for the failure message
                lines = source.splitlines()
                offending_line = lines[bad_line - 1] if bad_line - 1 < len(lines) else ""
                failures.append(
                    f"{module.relative_to(REPO_ROOT)}: nested backtick at line "
                    f"{bad_line} (inside template opened at line {template_line}). "
                    f"Line: {offending_line.strip()!r}"
                )
    if failures:
        raise AssertionError(
            "Nested backticks found inside innerHTML template literals — "
            "these will silently break WKWebView at runtime (the "
            "template literal closes early). Fix by removing the backticks or restructuring the "
            "template:\n\n  " + "\n  ".join(failures)
        )


def test_template_finder_self_check():
    """The _find_innerhtml_template_ranges helper itself works
    correctly. Pin the parser against simple synthetic cases so a
    future refactor of the lint logic can't silently break the
    invariant by mis-locating the template ranges."""
    # Single template, one line
    src = 'foo.innerHTML = `<div>hi</div>`;'
    ranges = _find_innerhtml_template_ranges(src)
    assert len(ranges) == 1, f"expected 1 template, got {len(ranges)}"
    start, end = ranges[0]
    assert src[start] == "`" and src[end] == "`"
    assert src[start + 1 : end] == "<div>hi</div>"

    # Two templates in same file
    src = 'a.innerHTML = `<a></a>`;\nb.innerHTML = `<b></b>`;'
    ranges = _find_innerhtml_template_ranges(src)
    assert len(ranges) == 2

    # Template with ${interpolation} that contains backticks INSIDE
    # the interpolation (e.g. nested sub-template) — those backticks
    # must NOT count as template-closing.
    src = 'a.innerHTML = `<div>${`nested`}</div>`;'
    ranges = _find_innerhtml_template_ranges(src)
    assert len(ranges) == 1, f"interpolation-with-nested-template confused parser: {ranges}"
    start, end = ranges[0]
    # The outer template's body includes the literal text "<div>${`nested`}</div>"
    body = src[start + 1 : end]
    assert body.startswith("<div>") and body.endswith("</div>")


def test_every_module_parses_cleanly_as_es_module():
    """A module with two `const stats = state.stats;` declarations in
    the same function scope must be caught. The
    duplicate `const` is a parse-time SyntaxError in ES modules — but
    plain `node --check FILE` accepts it (V8 defaults to script-mode
    parsing). The error only surfaces with `node --check
    --input-type=module < FILE`, which forces module-mode parsing and
    matches what WKWebView's JavaScriptCore does at runtime.

    Without this pin, a refactor that introduces such a name collision
    ships unnoticed. WKWebView fails to load the module on every
    launch, yet the empty-state codepath rarely runs in practice (most
    users have a non-empty workspace and queries that return hits) so
    the breakage stays silent until someone happens to open it.

    The pin runs `node --check --input-type=module < FILE` on every
    .js file under app/modules/ (excluding iCloud-dupe ` 2.js`
    siblings). Catches:

      - duplicate `const`/`let` declarations in the same scope
      - duplicate function-parameter names in strict mode
      - other syntax errors that node's script-mode parser is
        lenient about but module-mode is strict on
      - the existing nested-backtick class (V8 catches it in
        module mode even though it accepts it in script mode)

    Test SKIPS if `node` isn't on PATH (some CI sandboxes don't have
    it). dev_check.sh runs the same check so local dev catches it
    before commit; this pin is the CI safety net."""
    import shutil
    import subprocess

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node binary not on PATH — skipping ES-module parse-check pin")

    failures: list[str] = []
    for module in _gather_module_files():
        source = module.read_text(encoding="utf-8")
        # `node --check --input-type=module` consumes stdin and emits
        # the parse error to stderr with a non-zero exit. Capture both
        # so we can surface the actual offending line/column to the
        # test failure message.
        result = subprocess.run(
            [node_bin, "--check", "--input-type=module"],
            input=source,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            # node emits the error on stderr; first 3 lines are the
            # file/line context + the SyntaxError class + the carat.
            err = (result.stderr or result.stdout or "").strip()
            # Trim to first 6 lines so the failure message stays
            # readable when multiple modules break at once.
            err_excerpt = "\n      ".join(err.splitlines()[:6])
            failures.append(
                f"  {module.relative_to(REPO_ROOT)}:\n      {err_excerpt}"
            )

    if failures:
        raise AssertionError(
            "One or more app/modules/*.js files fail to parse as an "
            "ES module — WKWebView will fail to load them at runtime, "
            "breaking whatever UI surface depends on the module. Same "
            "class of bug as the duplicate-const in "
            "results.js that silently passes review. Fix "
            "the parse error reported below, then re-run the test.\n\n"
            "Offending modules:\n" + "\n".join(failures)
        )


def test_lint_catches_synthetic_nested_backtick():
    """Drop a synthetic nested-backtick string through the lint
    pipeline and confirm it gets flagged. Guards against a future
    "let's tighten the regex" change that accidentally narrows the
    detector and silently lets the next nested backtick slip through."""
    bad_src = (
        'function render(c) {\n'
        '  c.innerHTML = `\n'
        '    <!-- via `code.snippet()` comment -->\n'
        '  `;\n'
        '}\n'
    )
    ranges = _find_innerhtml_template_ranges(bad_src)
    assert len(ranges) == 1, "synthetic input should have exactly one template"
    start, end = ranges[0]
    bad_positions = _nested_backticks_in_template(bad_src, start, end)
    assert len(bad_positions) == 2, (
        f"synthetic input should flag 2 nested backticks (open + close of "
        f"the comment's `code.snippet()` wrap), got {len(bad_positions)}"
    )
