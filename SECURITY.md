# Security policy

Tern is source-visible and all rights reserved (see [LICENSE](LICENSE)).
There is no released or distributed build, so there are no supported release
versions: reports are about the code on `main`.

## Reporting a vulnerability

Please report a suspected vulnerability privately, through GitHub's private
vulnerability reporting: open the repository's **Security** tab and choose
**Report a vulnerability**
(<https://github.com/B0yko/tern/security/advisories/new>).

Please do not open a public issue for it. Include what you found, where in
the code, and steps to reproduce. Reports are read as time allows; there is
no service-level commitment.

## Scope

The local loopback API (`api/`), the desktop shell (`tauri/`), the licence
edge function (`license_server/`) and the build scripts are in scope. The
README's Status section lists the known limitations, and those are not new
findings. Reporting a vulnerability does not grant any right to use, copy or
redistribute the code beyond what the LICENSE and GitHub's Terms of Service
allow.

## Dependency advisories

The lockfiles were audited on 2026-10-23 (`pip-audit` for `api/uv.lock` and
`service_pipeline/uv.lock`, the OSV database for `tauri/src-tauri/Cargo.lock`)
and every advisory with a compatible fix was applied. Three Python packages
are still flagged, and none is used in the affected way:

- `chromadb`: no fixed release. The advisories concern Chroma's server mode
  (authorization and `trust_remote_code` on the server). Tern uses the
  embedded `PersistentClient` and never starts a Chroma server.
- `accelerate`: no fixed release. The advisory is a path traversal in its
  checkpoint-loading helpers (`load_checkpoint_in_model` and
  `load_checkpoint_and_dispatch`), which no Tern code calls.
- `oauthlib`: fixed only in 4.0.0. The advisory is a timing side-channel in
  its PKCE provider code, which Tern does not use. It is a transitive
  dependency.

In the Rust lockfile, the network-facing crates (`rustls`, `rustls-webpki`,
`tokio`, `webpki-root-certs`) were updated. Of the advisories that remain,
`zip`, `glib` and `proc-macro-error` are not in the dependency graph for
macOS (`cargo tree --target aarch64-apple-darwin -i <crate>` finds nothing).
The five `unic-*` crates are unmaintained-crate notices pulled in through
Tauri's `urlpattern`; a full `cargo update` drops them, but it also moves
Tauri itself, so it is a separate change.
