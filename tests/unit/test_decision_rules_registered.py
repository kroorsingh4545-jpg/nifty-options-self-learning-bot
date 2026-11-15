"""D7: the pre-registered rules cannot change silently."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from research.decision.rules import (
    EVALUATION_FROM,
    REGISTERED_FINGERPRINT,
    REGISTERED_ON,
    RULES_V1,
    RULES_VERSION,
    fingerprint,
)

DOC = Path(__file__).resolve().parents[2] / "docs" / "decision-engine-d7-preregistration.md"


def test_the_rules_match_their_registered_fingerprint() -> None:
    assert fingerprint(RULES_V1) == REGISTERED_FINGERPRINT, (
        "RULES_V1 changed: register a new version (new RULES_VERSION, document, fingerprint) instead")


def test_the_registration_document_records_the_same_fingerprint_and_version() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert REGISTERED_FINGERPRINT in text and RULES_VERSION in text


def test_the_evaluation_starts_after_the_registration_and_slots_are_inert() -> None:
    assert date(2026, 11, 15) == REGISTERED_ON and EVALUATION_FROM > REGISTERED_ON
    assert RULES_V1["slot_effect_in_v1"] == "none"
    assert set(RULES_V1["slots"]) == {"D5_THRUST", "D5_PAIRFLOW", "D6_CROSSFLOW"}


def test_any_change_changes_the_fingerprint() -> None:
    changed = {**RULES_V1, "max_breakeven_hit_rate": 0.56}
    assert fingerprint(changed) != REGISTERED_FINGERPRINT
