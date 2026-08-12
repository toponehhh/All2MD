from __future__ import annotations

import json

import pytest

from all2md.config import Settings, owner_id_for_tenant


def test_short_api_key_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("ALL2MD_API_KEY", "too-short")

    with pytest.raises(RuntimeError, match="at least 16"):
        Settings.from_env()


def test_rate_limit_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ALL2MD_RATE_LIMIT_PER_MINUTE", "0")

    assert Settings.from_env().rate_limit_per_minute == 0


def test_api_keys_json_maps_key_rotation_to_stable_tenant_owner(monkeypatch) -> None:
    monkeypatch.delenv("ALL2MD_API_KEY", raising=False)
    monkeypatch.setenv(
        "ALL2MD_API_KEYS_JSON",
        json.dumps(
            {
                "tenant-a": [
                    "tenant-a-key-version-one",
                    "tenant-a-key-version-two",
                ],
                "tenant-b": "tenant-b-production-key",
            }
        ),
    )

    settings = Settings.from_env()

    assert settings.authentication_required is True
    assert settings.authenticate_api_key("tenant-a-key-version-one") == owner_id_for_tenant(
        "tenant-a"
    )
    assert settings.authenticate_api_key("tenant-a-key-version-two") == owner_id_for_tenant(
        "tenant-a"
    )
    assert settings.authenticate_api_key("tenant-b-production-key") == owner_id_for_tenant(
        "tenant-b"
    )
    assert settings.authenticate_api_key("invalid-production-key") is None


def test_api_keys_json_rejects_duplicate_secrets(monkeypatch) -> None:
    monkeypatch.delenv("ALL2MD_API_KEY", raising=False)
    monkeypatch.setenv(
        "ALL2MD_API_KEYS_JSON",
        json.dumps(
            {
                "tenant-a": "shared-production-secret",
                "tenant-b": "shared-production-secret",
            }
        ),
    )

    with pytest.raises(RuntimeError, match="cannot be assigned more than once"):
        Settings.from_env()
