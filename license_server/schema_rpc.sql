-- Access path for the edge function.
--
-- `licensing` is deliberately not in PostgREST's exposed-schema list, so
-- supabase-js cannot read it directly — the first deploy failed with
-- PGRST106 for exactly that reason. Rather than exposing the schema, the
-- function reaches it through two narrow SECURITY DEFINER entry points in
-- `public`, executable only by service_role.
--
-- The seat rules still live in decide.ts, where they are tested. The cap
-- repeated inside tern_license_record is a backstop against the one thing
-- application-level logic cannot cover: two machines activating the last
-- seat at the same instant. It is not the primary rule. It only holds
-- because the function locks the licence row before it counts seats;
-- re-apply this file on an existing deployment to pick that lock up.

create or replace function public.tern_license_lookup(p_key text)
returns json
language sql
security definer
set search_path = licensing, pg_temp
as $$
  select json_build_object(
    'licence', (
      select to_jsonb(l) - 'note' - 'created_at'
      from licenses l where l.key = p_key
    ),
    'machines', coalesce(
      (select json_agg(m.machine_id) from license_machines m where m.license_key = p_key),
      '[]'::json
    )
  );
$$;

create or replace function public.tern_license_record(
    p_key text, p_machine text, p_app text
)
returns boolean
language plpgsql
security definer
set search_path = licensing, pg_temp
as $$
declare
    v_seats integer;
    v_used  integer;
begin
    -- Lock the licence row until this transaction ends. Every activation
    -- of the same key queues here, one at a time, so the count below
    -- always sees the seats taken by an activation that got in first.
    -- Without the lock, two new machines can both count the same number
    -- of seats under READ COMMITTED and both take the last one.
    select seats into v_seats from licenses where key = p_key for update;
    if v_seats is null then
        return false;
    end if;

    -- Already bound: refresh the heartbeat, never consume another seat.
    if exists (
        select 1 from license_machines
        where license_key = p_key and machine_id = p_machine
    ) then
        update license_machines
           set last_seen_at = now(), app_version = p_app
         where license_key = p_key and machine_id = p_machine;
        return true;
    end if;

    -- Safe to count now: the row lock above means no other activation of
    -- this key can insert between this count and the insert below.
    select count(*) into v_used from license_machines where license_key = p_key;
    if v_used >= v_seats then
        return false;
    end if;

    insert into license_machines (license_key, machine_id, app_version)
    values (p_key, p_machine, p_app)
    on conflict (license_key, machine_id) do update
        set last_seen_at = now(), app_version = excluded.app_version;
    return true;
end;
$$;

-- Only the edge function's key may call these. `public` in a REVOKE means
-- the PUBLIC pseudo-role, which is what functions are granted to by default.
revoke all on function public.tern_license_lookup(text) from public, anon, authenticated;
revoke all on function public.tern_license_record(text, text, text) from public, anon, authenticated;
grant execute on function public.tern_license_lookup(text) to service_role;
grant execute on function public.tern_license_record(text, text, text) to service_role;
