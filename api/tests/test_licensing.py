"""Trial quota + machine identity.

These are the rules that decide whether an unlicensed copy of Tern can
index another file. Before this module existed the licence was decorative:
`/api/license/status` rendered a badge and nothing anywhere asked whether
the user had paid, so the product was fully functional without a key.

Everything here is pure except load_trial / save_trial, so the rules can be
tested without touching the filesystem or the API.
"""
import json

import pytest

from licensing import (
    TRIAL_LIMIT_MS,
    check_quota,
    load_trial,
    machine_id,
    record_usage,
    save_trial,
    trial_state,
)

MINUTE = 60_000


def fresh() -> dict:
    return {"used_ms": 0}


# --- quota accounting ---------------------------------------------------

def test_fresh_trial_has_the_full_limit_available():
    state = trial_state(fresh(), licensed=False)
    assert state["licensed"] is False
    assert state["limit_ms"] == TRIAL_LIMIT_MS
    assert state["used_ms"] == 0
    assert state["remaining_ms"] == TRIAL_LIMIT_MS
    assert state["exhausted"] is False


def test_recording_usage_reduces_what_is_left():
    trial = record_usage(fresh(), 30 * MINUTE)
    state = trial_state(trial, licensed=False)
    assert state["used_ms"] == 30 * MINUTE
    assert state["remaining_ms"] == TRIAL_LIMIT_MS - 30 * MINUTE
    assert state["exhausted"] is False


def test_usage_accumulates_across_calls():
    trial = record_usage(record_usage(fresh(), 10 * MINUTE), 5 * MINUTE)
    assert trial_state(trial, licensed=False)["used_ms"] == 15 * MINUTE


def test_trial_is_exhausted_once_the_limit_is_reached():
    trial = record_usage(fresh(), TRIAL_LIMIT_MS)
    state = trial_state(trial, licensed=False)
    assert state["exhausted"] is True
    assert state["remaining_ms"] == 0


def test_remaining_never_goes_negative():
    """A file can overshoot the limit slightly if it was admitted while
    quota remained. Remaining clamps at zero rather than reporting a
    negative number to the UI."""
    trial = record_usage(fresh(), TRIAL_LIMIT_MS + 99 * MINUTE)
    assert trial_state(trial, licensed=False)["remaining_ms"] == 0


def test_negative_duration_cannot_hand_quota_back():
    """Guards the obvious tamper: a crafted duration that credits the
    trial instead of spending it."""
    trial = record_usage(record_usage(fresh(), 40 * MINUTE), -30 * MINUTE)
    assert trial_state(trial, licensed=False)["used_ms"] == 40 * MINUTE


# --- the gate itself ----------------------------------------------------

def test_a_file_within_the_remaining_quota_is_allowed():
    allowed, reason = check_quota(fresh(), licensed=False, duration_ms=45 * MINUTE)
    assert allowed is True
    assert reason is None


def test_a_file_is_refused_once_the_trial_is_spent():
    trial = record_usage(fresh(), TRIAL_LIMIT_MS)
    allowed, reason = check_quota(trial, licensed=False, duration_ms=1 * MINUTE)
    assert allowed is False
    assert reason and "trial" in reason.lower()


def test_a_single_file_longer_than_the_whole_limit_is_refused_outright():
    """Refuse before starting rather than transcribing 40 of 300 minutes
    and stopping. A half-indexed file is worse than a clear refusal."""
    allowed, reason = check_quota(fresh(), licensed=False,
                                  duration_ms=TRIAL_LIMIT_MS + MINUTE)
    assert allowed is False
    assert reason and "trial" in reason.lower()


def test_the_refusal_message_names_the_limit_in_minutes():
    """The user has to be able to act on this without reading the source."""
    trial = record_usage(fresh(), TRIAL_LIMIT_MS)
    _, reason = check_quota(trial, licensed=False, duration_ms=MINUTE)
    assert str(TRIAL_LIMIT_MS // MINUTE) in reason


def test_photos_and_other_zero_duration_media_are_always_allowed():
    """Images carry no duration. Charging them nothing keeps the trial
    honest about what it is metering, which is transcription time."""
    trial = record_usage(fresh(), TRIAL_LIMIT_MS)
    allowed, reason = check_quota(trial, licensed=False, duration_ms=0)
    assert allowed is True
    assert reason is None


def test_zero_duration_media_does_not_spend_quota():
    trial = record_usage(fresh(), 0)
    assert trial_state(trial, licensed=False)["used_ms"] == 0


# --- licensed copies bypass all of it ------------------------------------

def test_a_licensed_copy_reports_no_limit():
    state = trial_state(fresh(), licensed=True)
    assert state["licensed"] is True
    assert state["limit_ms"] is None
    assert state["remaining_ms"] is None
    assert state["exhausted"] is False


def test_a_licensed_copy_indexes_past_an_exhausted_trial():
    trial = record_usage(fresh(), TRIAL_LIMIT_MS * 10)
    allowed, reason = check_quota(trial, licensed=True, duration_ms=600 * MINUTE)
    assert allowed is True
    assert reason is None


# --- persistence ---------------------------------------------------------

def test_trial_survives_a_save_and_load(tmp_path):
    path = tmp_path / "trial.json"
    save_trial(path, record_usage(fresh(), 12 * MINUTE))
    assert trial_state(load_trial(path), licensed=False)["used_ms"] == 12 * MINUTE


def test_a_missing_trial_file_starts_fresh(tmp_path):
    """First launch. No file yet is the expected state, not an error."""
    state = trial_state(load_trial(tmp_path / "nope.json"), licensed=False)
    assert state["used_ms"] == 0
    assert state["exhausted"] is False


def test_a_corrupt_trial_file_fails_closed(tmp_path):
    """Deleting or mangling trial.json must not hand out a fresh trial,
    or resetting the quota is a one-line shell command. Licensed users
    are unaffected because the licence is checked before the quota."""
    path = tmp_path / "trial.json"
    path.write_text("{not json at all")
    state = trial_state(load_trial(path), licensed=False)
    assert state["exhausted"] is True
    allowed, reason = check_quota(load_trial(path), licensed=False, duration_ms=MINUTE)
    assert allowed is False
    assert reason


def test_a_trial_file_with_a_missing_counter_fails_closed(tmp_path):
    """Valid JSON, wrong shape — same tamper surface as corrupt bytes."""
    path = tmp_path / "trial.json"
    path.write_text(json.dumps({"something_else": 1}))
    assert trial_state(load_trial(path), licensed=False)["exhausted"] is True


def test_saving_is_atomic_enough_to_survive_a_reread(tmp_path):
    path = tmp_path / "trial.json"
    for minutes in (5, 10, 15):
        save_trial(path, record_usage(load_trial(path) if path.exists() else fresh(),
                                      minutes * MINUTE))
    assert trial_state(load_trial(path), licensed=False)["used_ms"] == 30 * MINUTE


# --- machine identity -----------------------------------------------------

def test_machine_id_is_a_stable_non_empty_string():
    first = machine_id()
    assert isinstance(first, str)
    assert first.strip()
    assert machine_id() == first


def test_machine_id_is_long_enough_to_be_a_real_identifier():
    """Seat counting keys off this. A short or obviously-shared value
    would collapse every customer machine into one seat."""
    assert len(machine_id()) >= 16
