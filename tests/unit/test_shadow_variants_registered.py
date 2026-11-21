"""Shadow variants sv.v1: the pre-registered rules cannot change silently."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from research.shadow.variant_rules import (
    EVALUATION_FROM,
    REGISTERED_FINGERPRINT,
    REGISTERED_ON,
    VARIANTS_V1,
    VARIANTS_VERSION,
    fingerprint,
)

DOC = Path(__file__).resolve().parents[2] / "docs" / "shadow-variants-preregistration.md"


def test_the_rules_match_their_registered_fingerprint() -> None:
    assert fingerprint(VARIANTS_V1) == REGISTERED_FINGERPRINT, (
        "VARIANTS_V1 changed: register a new version (new VARIANTS_VERSION, document, fingerprint) instead")


def test_the_registration_document_records_the_same_fingerprint_and_version() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert REGISTERED_FINGERPRINT in text and VARIANTS_VERSION in text


def test_the_evaluation_is_forward_only() -> None:
    assert date(2026, 11, 21) == REGISTERED_ON and EVALUATION_FROM > REGISTERED_ON
    assert set(VARIANTS_V1) >= {"A", "B", "common", "evaluation"}
    assert VARIANTS_V1["A"]["target"] is None and VARIANTS_V1["A"]["trailing"] is None
    assert VARIANTS_V1["common"]["lots"] == 1


def test_any_change_changes_the_fingerprint() -> None:
    assert fingerprint({**VARIANTS_V1, "version": "sv.v2"}) != REGISTERED_FINGERPRINT
