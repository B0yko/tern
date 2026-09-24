-- Tern licensing. Apply to the Supabase project that serves license-validate,
-- then apply schema_rpc.sql.
--
-- Two tables in their own schema, deliberately kept out of `public` so
-- PostgREST does not expose them: the edge function reaches them with the
-- service-role key, and nothing else should reach them at all.

create schema if not exists licensing;

create table if not exists licensing.licenses (
    key           text primary key,
    email         text        not null,
    seats         integer     not null default 1 check (seats >= 0),
    status        text        not null default 'active'
                              check (status in ('active', 'revoked')),
    purchased_at  timestamptz not null default now(),
    -- null means perpetual. A pilot gets a date; a bought licence usually
    -- does not.
    expires_at    timestamptz,
    note          text,
    created_at    timestamptz not null default now()
);

create table if not exists licensing.license_machines (
    license_key   text        not null
                              references licensing.licenses(key) on delete cascade,
    machine_id    text        not null,
    app_version   text,
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    primary key (license_key, machine_id)
);

create index if not exists license_machines_key_idx
    on licensing.license_machines (license_key);

-- Belt and braces. The schema is not in the exposed list, but if it ever
-- gets added by accident, RLS with no policies denies everything to anon
-- and authenticated. service_role bypasses RLS, which is how the edge
-- function still works.
alter table licensing.licenses          enable row level security;
alter table licensing.license_machines  enable row level security;

revoke all on schema licensing from anon, authenticated;
revoke all on all tables in schema licensing from anon, authenticated;


-- ── operating it by hand ────────────────────────────────────────────────
-- Issue a three-month, three-seat pilot:
--
--   insert into licensing.licenses (key, email, seats, expires_at, note)
--   values ('TERN-XXXX-XXXX-XXXX', 'buyer@studio.example', 3,
--           now() + interval '3 months', 'pilot, invoice #001');
--
-- See who is using it:
--
--   select machine_id, app_version, first_seen_at, last_seen_at
--   from licensing.license_machines where license_key = 'TERN-XXXX-XXXX-XXXX';
--
-- Free a seat when a customer replaces a Mac:
--
--   delete from licensing.license_machines
--   where license_key = 'TERN-XXXX-XXXX-XXXX' and machine_id = '<hash>';
--
-- Stop a licence working (refunds, non-payment):
--
--   update licensing.licenses set status = 'revoked' where key = '...';
--
-- Revocation only bites on the next activation. The desktop app caches a
-- successful validation and keeps working offline by design, so treat this
-- as "no new machines and no re-activation", not as a kill switch.
