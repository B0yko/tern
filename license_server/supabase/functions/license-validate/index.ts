// Tern licence validation — Supabase Edge Function.
//
// One endpoint. The desktop app POSTs here from
// api/main.py::license_activate; the contract it expects is
//
//   POST  { license_key, app_version, machine_id }
//   200   { is_valid, email, purchased_at, message, seats, seats_used }
//
// Always answers 200 with is_valid=false rather than a 4xx for a bad key.
// The client treats a transport failure as "keep the cached state" so a
// paying customer is never locked out by an outage — returning 4xx for a
// wrong key would land in that same branch and look like downtime.
//
// The seat rules live in decide.ts and are tested there.

import { createClient } from "jsr:@supabase/supabase-js@2";
import { decide, type License } from "./decide.ts";

const url = Deno.env.get("SUPABASE_URL")!;
const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") {
    return json({ is_valid: false, message: "POST a licence key to this endpoint." }, 405);
  }

  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return json({ is_valid: false, message: "Malformed request body." }, 400);
  }

  const key = String(body.license_key ?? "").trim();
  const machineId = String(body.machine_id ?? "").trim();
  const appVersion = String(body.app_version ?? "").slice(0, 32);

  // Mirrors the client-side cap in LicenseActivateRequest. Without it a
  // hostile POST can hand the database a megabyte to index against.
  if (!key || key.length > 256) {
    return json({ is_valid: false, message: "Licence key missing or malformed." });
  }
  if (machineId.length > 128) {
    return json({ is_valid: false, message: "Machine identifier malformed." });
  }

  const db = createClient(url, serviceKey, { auth: { persistSession: false } });

  // The licensing schema is not in PostgREST's exposed list on purpose, so
  // it cannot be read as a table even with the service key — the first
  // deploy failed with PGRST106 for exactly that reason. Two narrow
  // SECURITY DEFINER functions in `public`, executable only by
  // service_role, are the way in. See schema_rpc.sql.
  const { data: looked, error: lookupError } = await db.rpc("tern_license_lookup", {
    p_key: key,
  });

  if (lookupError) {
    console.error("licence lookup failed", lookupError);
    // 503 lands in the client's transport-failure branch, which preserves
    // whatever it had cached. That is the right outcome for our outage.
    return json({ is_valid: false, message: "Licence service unavailable." }, 503);
  }

  const licence = (looked?.licence ?? null) as License | null;
  const bound = (looked?.machines ?? []) as string[];
  const verdict = decide(licence, bound, machineId, new Date());

  if (verdict.is_valid) {
    // Idempotent: binds a new seat, or refreshes the heartbeat on a seat
    // this machine already holds. Returns false only when a concurrent
    // activation took the last seat between our read and this write.
    const { data: recorded, error: recordError } = await db.rpc("tern_license_record", {
      p_key: key,
      p_machine: machineId,
      p_app: appVersion,
    });
    if (recordError) {
      console.error("seat registration failed", recordError);
      return json({ is_valid: false, message: "Licence service unavailable." }, 503);
    }
    if (recorded === false) {
      return json({
        is_valid: false,
        email: null,
        purchased_at: null,
        message: `All ${verdict.seats} seats on this licence are in use. ` +
          `Release a machine or add a seat, then activate again.`,
        seats: verdict.seats,
        seats_used: verdict.seats,
      });
    }
  }

  // `register` is an internal instruction to this handler, not something
  // the desktop app should ever see.
  const { register: _drop, ...response } = verdict;
  return json(response);
});
