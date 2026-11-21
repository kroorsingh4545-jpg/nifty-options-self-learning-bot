"""ORB study E1: the pre-registered design cannot change silently."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from research.orb_study.registration import (
    FORWARD_FROM,
    REGISTERED_FINGERPRINT,
    REGISTERED_ON,
    STUDY_E1,
    STUDY_VERSION,
    fingerprint,
)

DOC = Path(__file__).resolve().parents[2] / "docs" / "orb-e1-preregistration.md"


def test_the_design_matches_its_registered_fingerprint() -> None:
    assert fingerprint(STUDY_E1) == REGISTERED_FINGERPRINT, (
        "STUDY_E1 changed: register a new version (new STUDY_VERSION, document, fingerprint) instead")


def test_the_registration_document_records_the_same_fingerprint_and_version() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert REGISTERED_FINGERPRINT in text and STUDY_VERSION in text


def test_registration_dates_and_scope() -> None:
    assert date(2026, 11, 21) == REGISTERED_ON and FORWARD_FROM >= REGISTERED_ON
    assert STUDY_E1["data"]["years_tested"] == [2022, 2023, 2024, 2025]
    assert set(STUDY_E1["hypotheses"]) == {f"H{i}" for i in range(1, 10)}
    assert STUDY_E1["data"]["lot_size"] == 65


def test_any_change_changes_the_fingerprint() -> None:
    changed = {**STUDY_E1, "version": "e1.v2"}
    assert fingerprint(changed) != REGISTERED_FINGERPRINT
