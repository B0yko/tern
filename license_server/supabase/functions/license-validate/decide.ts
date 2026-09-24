// Seat rules for Tern licences.
//
// Pure on purpose: the handler fetches the licence row and the machine list,
// calls this, then performs whatever write the verdict asks for. Keeping the
// IO out means every rule below is testable, which matters because these are
// the rules a paying customer runs into on a bad day.

export interface License {
  key: string;
  email: string;
  seats: number;
  status: "active" | "revoked" | string;
  purchased_at: string | null;
  expires_at: string | null; // null = perpetual
}

export interface Verdict {
  is_valid: boolean;
  /** True when the caller should bind this machine to the licence. */
  register: boolean;
  email: string | null;
  purchased_at: string | null;
  message: string;
  seats: number;
  seats_used: number;
}

function refuse(
  message: string,
  seats = 0,
  seats_used = 0,
): Verdict {
  return {
    is_valid: false,
    register: false,
    email: null,
    purchased_at: null,
    message,
    seats,
    seats_used,
  };
}

export function decide(
  licence: License | null,
  boundMachines: string[],
  machineId: string,
  now: Date,
): Verdict {
  if (!licence) {
    // Deliberately vague: a precise "no such key" versus "wrong key for
    // this machine" would let someone probe which keys exist.
    return refuse("Licence key not recognised. Check it for typos, or reply to your invoice and we will re-issue it.");
  }

  if (!machineId || !machineId.trim()) {
    return refuse(
      "This copy did not identify its machine, so the licence cannot be assigned to a seat. Update to a current build.",
      licence.seats,
      boundMachines.length,
    );
  }

  if (licence.status !== "active") {
    return refuse(
      `This licence has been ${licence.status}. Get in touch if that is unexpected.`,
      licence.seats,
      boundMachines.length,
    );
  }

  if (licence.expires_at && new Date(licence.expires_at) <= now) {
    return refuse(
      "This licence has expired. Renew it to keep indexing — everything already indexed stays searchable.",
      licence.seats,
      boundMachines.length,
    );
  }

  const known = boundMachines.includes(machineId);

  // A machine already on the licence always re-activates, even when the
  // licence is full. Otherwise wiping and reinstalling a Mac locks the
  // customer out of a seat they already own, which is the worst possible
  // moment to argue about seat counts.
  if (known) {
    return {
      is_valid: true,
      register: false,
      email: licence.email,
      purchased_at: licence.purchased_at,
      message: "Licence active.",
      seats: licence.seats,
      seats_used: boundMachines.length,
    };
  }

  if (boundMachines.length >= licence.seats) {
    return refuse(
      `All ${licence.seats} seat${licence.seats === 1 ? "" : "s"} on this licence are in use. ` +
        `Release a machine or add a seat, then activate again.`,
      licence.seats,
      boundMachines.length,
    );
  }

  return {
    is_valid: true,
    register: true,
    email: licence.email,
    purchased_at: licence.purchased_at,
    message: "Licence active.",
    seats: licence.seats,
    seats_used: boundMachines.length + 1,
  };
}
