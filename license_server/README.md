# license_server

Validates Tern licence keys and counts seats. One Supabase Edge Function and
two tables.

The desktop app calls it from `api/main.py::license_activate`:

```
POST {TERN_LICENSE_SERVER}
     { "license_key": "...", "app_version": "0.1.0", "machine_id": "<sha256>" }
200  { "is_valid": true, "email": "...", "purchased_at": "...",
       "message": "Licence active.", "seats": 3, "seats_used": 1 }
```

`TERN_LICENSE_SERVER` is the endpoint itself, not a prefix.

`machine_id` is a SHA-256 of the Mac's `IOPlatformUUID` with a fixed app
prefix, computed in `api/licensing.py`. Hashed rather than raw because the
server needs to tell machines apart, not identify them.

## Files

| File | What it is |
|---|---|
| `supabase/functions/license-validate/decide.ts` | The seat rules. Pure — no database, no network. |
| `decide_test.ts` | 13 tests over those rules. `deno test decide_test.ts` |
| `supabase/functions/license-validate/index.ts` | The HTTP handler. Looks up, calls `decide`, records the seat. |
| `schema.sql` | Tables, RLS, and the hand-operation queries. |
| `schema_rpc.sql` | The two SECURITY DEFINER entry points the function reaches the hidden schema through. |
| `verify_deployment.sh` | Four live checks against a deployed endpoint. |

## Deploying

Target is a Supabase project of your own. `<project-ref>` below stands for
its ref.

**Schema: applied to a test project on 30 July 2026.** Both tables exist,
RLS is on, `anon` and `authenticated` have no grants, and PostgREST answers
406 for the `licensing` schema because it is not in the exposed list. Re-running `schema.sql` is
safe; every statement is idempotent.

Apply `schema_rpc.sql` after it. On an existing deployment, re-apply
`schema_rpc.sql` as well: the row lock that stops two machines taking the
last seat lives in `tern_license_record`, and only takes effect once that
function is replaced.

**Function: deployed to that test project on 30 July 2026**, slug
`license-validate`, `verify_jwt` off. It is served at

    https://<project-ref>.supabase.co/functions/v1/license-validate

To ship a change:

```bash
supabase login                      # or: export SUPABASE_ACCESS_TOKEN=sbp_...
supabase functions deploy license-validate \
  --project-ref <project-ref> \
  --no-verify-jwt --use-api --workdir license_server
```

Run it from the repository root. `--use-api` bundles server-side so Docker
is not needed; `--workdir` points at this directory because that is where
`supabase/config.toml` lives.

`--no-verify-jwt` is deliberate. The desktop app posts without an
Authorization header, and the licence key is itself the credential — the
function refuses anything it does not recognise. Requiring a JWT as well
would mean shipping the anon key inside the app bundle for no added
protection.

`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are injected by the platform;
nothing else needs setting.

Then prove it works:

```bash
license_server/verify_deployment.sh \
  https://<project-ref>.supabase.co/functions/v1/license-validate \
  <smoke-test-key>
```

`<smoke-test-key>` is a licence row issued for this check, the same way as
in "Issuing a pilot by hand" below. The script takes one seat on it and
prints the SQL that releases it. Never put a working key in this file.

The desktop app reads the endpoint from `TERN_LICENSE_SERVER` (see
`api/main.py`). The setting is the **full endpoint**, not a prefix — a
Supabase function is served at `/functions/v1/<name>` and cannot host a
`/api/license/validate` path of its own. There is no default in the source:
a build has to set it for the API sidecar. An activation request can also
carry its own `server_url`, which takes precedence; with neither, activation
answers with a message naming the setting.

## Two behaviours worth knowing

**An outage does not lock anyone out.** The client treats an unreachable
server as "keep the cached state", so a licensed copy keeps working. That
is why a bad key returns `200 {is_valid: false}` rather than a 4xx: a 4xx
would land in the same transport-failure branch and read as downtime.

**Revocation is not a kill switch.** A successful validation is cached on the
machine and the app works offline by design. Setting `status = 'revoked'`
stops new activations and re-activations. It does not reach into a machine
that has already been validated.

## Issuing a pilot by hand

There is no checkout, and for a handful of pilots there does not need to be.

```sql
insert into licensing.licenses (key, email, seats, expires_at, note)
values ('TERN-XXXX-XXXX-XXXX', 'buyer@studio.example', 3,
        now() + interval '3 months', 'pilot, invoice #001');
```

Generate keys with something unguessable, not a counter:

```bash
python3 -c "import secrets; print('TERN-' + '-'.join(secrets.token_hex(2).upper() for _ in range(3)))"
```

Then check on it:

```sql
select m.machine_id, m.app_version, m.first_seen_at, m.last_seen_at
from licensing.license_machines m
where m.license_key = 'TERN-XXXX-XXXX-XXXX';
```

## What this does not do

No checkout, no invoicing, no email delivery, no self-serve seat management.
All of that is a spreadsheet and a mail client until there are enough
licences to make it annoying.
