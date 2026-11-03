"""Credential masking."""

from __future__ import annotations

import pytest

from core.masking import MASK, SecretMasker, mask_secret


def test_mask_secret_partial_for_long_values() -> None:
    assert mask_secret("abcdefghijklmnopwxyz") == "abcd****wxyz"


@pytest.mark.parametrize("value", ["short", "fifteen_chars__"])
def test_mask_secret_full_for_short_values(value: str) -> None:
    assert mask_secret(value) == MASK


def test_mask_secret_empty() -> None:
    assert mask_secret(None) == ""
    assert mask_secret("") == ""


def test_registered_secret_is_masked_anywhere() -> None:
    m = SecretMasker()
    secret = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
    m.register(secret)
    out = m.mask_text(f"prefix{secret}suffix and again {secret}")
    assert secret not in out


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.abc.def",
        "https://api.example.com/login?api_key=k3y-value-123&code=1",
        '{"access_token": "tok-abcdef-123456"}',
        "client_secret=s3cr3t-value",
        "password: hunter2hunter2",
        "telegram 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ end",
    ],
)
def test_pattern_masking_of_unregistered_secrets(text: str) -> None:
    out = SecretMasker().mask_text(text)
    for leaked in (
        "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9",
        "k3y-value-123",
        "tok-abcdef-123456",
        "s3cr3t-value",
        "hunter2hunter2",
        "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ",
    ):
        assert leaked not in out


def test_mask_obj_masks_sensitive_keys_and_nested_values() -> None:
    m = SecretMasker()
    m.register("registered-secret-value")
    obj = {
        "api_key": "short",
        "headers": {"Authorization": "Bearer abc.def.ghi"},
        "items": ["ok", "has registered-secret-value inside"],
        "count": 3,
    }
    out = m.mask_obj(obj)
    assert out["api_key"] == MASK
    assert out["headers"]["Authorization"] == MASK
    assert "registered-secret-value" not in out["items"][1]
    assert out["count"] == 3
    assert out["items"][0] == "ok"


def test_too_short_values_are_not_registered() -> None:
    m = SecretMasker()
    m.register("abc", None, "")
    assert m.mask_text("abc def") == "abc def"


def test_plain_text_is_unchanged() -> None:
    text = "order PAPER-orb_options-abc placed for NIFTY 24500 CE qty 75"
    assert SecretMasker().mask_text(text) == text
