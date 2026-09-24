// The seat rules, tested without a database.
//
// `decide` is deliberately pure: it takes the licence row and the machines
// already bound to it, and returns the verdict plus whether this machine
// needs registering. The handler does the IO. That split is the only reason
// these cases can be written at all.
import { assertEquals } from "jsr:@std/assert@1";
import { decide, type License, type Verdict } from "./supabase/functions/license-validate/decide.ts";

const NOW = new Date("2026-07-30T12:00:00Z");

function licence(over: Partial<License> = {}): License {
  return {
    key: "TERN-AAAA-BBBB",
    email: "buyer@studio.example",
    seats: 3,
    status: "active",
    purchased_at: "2026-01-01T00:00:00Z",
    expires_at: null,
    ...over,
  };
}

Deno.test("an unknown key is refused", () => {
  const v = decide(null, [], "machine-1", NOW);
  assertEquals(v.is_valid, false);
  assertEquals(v.register, false);
  assertEquals(v.message.toLowerCase().includes("not recognised"), true);
});

Deno.test("a fresh machine takes a free seat", () => {
  const v = decide(licence(), [], "machine-1", NOW);
  assertEquals(v.is_valid, true);
  assertEquals(v.register, true);
  assertEquals(v.seats_used, 1, "the seat it just took counts");
  assertEquals(v.email, "buyer@studio.example");
});

Deno.test("a machine already bound to the key re-activates without taking another seat", () => {
  const v = decide(licence({ seats: 1 }), ["machine-1"], "machine-1", NOW);
  assertEquals(v.is_valid, true);
  assertEquals(v.register, false, "already registered, nothing to write");
  assertEquals(v.seats_used, 1);
});

Deno.test("reinstalling on a known machine works even when every seat is taken", () => {
  // The customer wipes a Mac and reinstalls. Refusing here would be the
  // single most infuriating way for this to fail.
  const v = decide(licence({ seats: 2 }), ["machine-1", "machine-2"], "machine-2", NOW);
  assertEquals(v.is_valid, true);
  assertEquals(v.register, false);
});

Deno.test("a new machine is refused once the seats are full", () => {
  const v = decide(licence({ seats: 2 }), ["machine-1", "machine-2"], "machine-3", NOW);
  assertEquals(v.is_valid, false);
  assertEquals(v.register, false);
  assertEquals(v.message.includes("2"), true, "say how many seats there are");
});

Deno.test("the refusal for a full licence says how to fix it", () => {
  const v = decide(licence({ seats: 1 }), ["machine-1"], "machine-2", NOW);
  assertEquals(v.is_valid, false);
  const m = v.message.toLowerCase();
  assertEquals(m.includes("seat"), true);
});

Deno.test("a revoked licence is refused even on a machine it knows", () => {
  const v = decide(licence({ status: "revoked" }), ["machine-1"], "machine-1", NOW);
  assertEquals(v.is_valid, false);
  assertEquals(v.message.toLowerCase().includes("revoked"), true);
});

Deno.test("an expired licence is refused", () => {
  const v = decide(
    licence({ expires_at: "2026-06-30T00:00:00Z" }),
    ["machine-1"],
    "machine-1",
    NOW,
  );
  assertEquals(v.is_valid, false);
  assertEquals(v.message.toLowerCase().includes("expired"), true);
});

Deno.test("a licence expiring later today is still valid", () => {
  const v = decide(
    licence({ expires_at: "2026-07-30T23:59:00Z" }),
    [],
    "machine-1",
    NOW,
  );
  assertEquals(v.is_valid, true);
});

Deno.test("a licence with no expiry never expires", () => {
  const v = decide(licence({ expires_at: null }), [], "machine-1",
    new Date("2099-01-01T00:00:00Z"));
  assertEquals(v.is_valid, true);
});

Deno.test("a missing machine id is refused rather than silently granted", () => {
  // An old client, or someone poking the endpoint by hand. Granting a
  // licence to an unidentifiable machine defeats seat counting entirely.
  const v = decide(licence(), [], "", NOW);
  assertEquals(v.is_valid, false);
  assertEquals(v.message.toLowerCase().includes("machine"), true);
});

Deno.test("seat count of zero refuses everything", () => {
  const v = decide(licence({ seats: 0 }), [], "machine-1", NOW);
  assertEquals(v.is_valid, false);
});

Deno.test("the verdict never echoes the licence key back", () => {
  // The response is logged on the client. Nothing here should carry the
  // secret it was asked about.
  const v: Verdict = decide(licence(), [], "machine-1", NOW);
  assertEquals(JSON.stringify(v).includes("TERN-AAAA-BBBB"), false);
});
