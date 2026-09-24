"""Trial quota and machine identity.

The rules that decide whether an unlicensed copy may index another file.
Kept free of FastAPI and of filesystem layout so they can be tested
directly; `main.py` owns where the state file lives and when to consult it.

Design notes:

* The trial meters **media duration**, not calendar days. An editor
  evaluating this needs to run it on a real episode, and a 14-day clock
  either expires while they are busy or hands over unlimited use to
  someone who is not. Duration bites exactly when the tool starts doing
  real work.
* Photos cost nothing. What is expensive is transcription, and metering
  images would make the limit feel arbitrary.
* A file that does not fit in what is left is refused whole. Transcribing
  40 minutes of a 300-minute file and stopping leaves a half-indexed
  archive, which is worse for the user than a clear refusal.
* Corrupt or reshaped state fails closed. Otherwise `rm trial.json` is the
  crack. Licensed copies never reach the quota check, so a paying customer
  cannot be locked out by a damaged file.

None of this stops a determined attacker — the enforcement is Python
inside a bundle they control. It stops casual non-payment, which is the
realistic behaviour of a business that has to account for its software.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

#: How much media an unlicensed copy may index, in total, ever.
TRIAL_LIMIT_MS = 120 * 60_000  # two hours

_machine_id_cache: str | None = None


# --- trial state ---------------------------------------------------------

def _used_ms(trial: dict) -> int | None:
    """Return recorded usage, or None when the state is unusable."""
    if not isinstance(trial, dict):
        return None
    used = trial.get("used_ms")
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        return None
    return used


def trial_state(trial: dict, licensed: bool) -> dict:
    """Summarise the quota for the UI and for check_quota."""
    if licensed:
        return {
            "licensed": True,
            "limit_ms": None,
            "used_ms": _used_ms(trial) or 0,
            "remaining_ms": None,
            "exhausted": False,
        }
    used = _used_ms(trial)
    if used is None:
        # Unreadable state. Fail closed — see the module docstring.
        return {
            "licensed": False,
            "limit_ms": TRIAL_LIMIT_MS,
            "used_ms": TRIAL_LIMIT_MS,
            "remaining_ms": 0,
            "exhausted": True,
            "unreadable": True,
        }
    return {
        "licensed": False,
        "limit_ms": TRIAL_LIMIT_MS,
        "used_ms": used,
        "remaining_ms": max(0, TRIAL_LIMIT_MS - used),
        "exhausted": used >= TRIAL_LIMIT_MS,
    }


def record_usage(trial: dict, duration_ms: int) -> dict:
    """Charge `duration_ms` against the trial and return the new state.

    Negative durations are ignored rather than credited, so a crafted or
    corrupt probe result cannot refill the quota.
    """
    used = _used_ms(trial)
    if used is None:
        used = TRIAL_LIMIT_MS
    spend = duration_ms if isinstance(duration_ms, int) and duration_ms > 0 else 0
    out = dict(trial) if isinstance(trial, dict) else {}
    out["used_ms"] = used + spend
    out.pop("unreadable", None)
    return out


def check_quota(trial: dict, licensed: bool, duration_ms: int) -> tuple[bool, str | None]:
    """May a file of this duration be indexed? Returns (allowed, reason)."""
    if licensed:
        return True, None
    if not isinstance(duration_ms, int) or duration_ms <= 0:
        return True, None  # photos and anything without a timeline

    state = trial_state(trial, licensed=False)
    limit_min = TRIAL_LIMIT_MS // 60_000

    if state["exhausted"]:
        return False, (
            f"Trial limit reached. Tern indexes up to {limit_min} minutes of "
            f"media without a licence key, and this copy has used all of it. "
            f"Enter a licence key to keep indexing — everything already "
            f"indexed stays searchable."
        )

    if duration_ms > state["remaining_ms"]:
        need_min = duration_ms // 60_000
        left_min = state["remaining_ms"] // 60_000
        return False, (
            f"This file is {need_min} minutes long and the trial has "
            f"{left_min} of its {limit_min} minutes left. Tern refuses a file "
            f"it cannot finish rather than indexing part of it. Enter a "
            f"licence key to index the whole thing."
        )

    return True, None


# --- persistence ---------------------------------------------------------

def load_trial(path: Path) -> dict:
    """Read trial state. Absent file is a fresh trial; damaged file is not."""
    path = Path(path)
    if not path.exists():
        return {"used_ms": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"unreadable": True}
    if _used_ms(data) is None:
        return {"unreadable": True}
    return data


def save_trial(path: Path, trial: dict) -> None:
    """Write trial state atomically, so a kill mid-write cannot truncate it
    into the unreadable state that fails closed on the next launch."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(trial, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- machine identity -----------------------------------------------------

def _raw_machine_identity() -> str:
    """Best available stable identifier for this Mac."""
    try:
        out = subprocess.run(
            ["/usr/sbin/ioreg", "-d2", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            if "IOPlatformUUID" in line and '"' in line:
                return line.rsplit('"', 2)[-2]
    except Exception:
        pass
    # Non-macOS, or ioreg unavailable: fall back to the NIC-derived node id.
    # Weaker, but it only has to be stable on the machine it runs on.
    import uuid as _uuid
    return f"node-{_uuid.getnode():x}"


def machine_id() -> str:
    """A stable, non-reversible identifier for seat counting.

    Hashed rather than sent raw: the licence server needs to tell machines
    apart, not to know which Mac this is.
    """
    global _machine_id_cache
    if _machine_id_cache is None:
        raw = _raw_machine_identity()
        _machine_id_cache = hashlib.sha256(f"tern:{raw}".encode()).hexdigest()
    return _machine_id_cache
