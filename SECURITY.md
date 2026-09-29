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
