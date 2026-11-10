"""Plan 0001 — detect & alert on unavailable Venice models.

These tests are written test-first. They cover:
- pure detection of configured models missing from an available set
- the startup validator's logging behaviour (loud for a missing primary)
- not crying wolf when the available list can't be determined
- HTTP error classification so 402/401/404 are distinguishable in logs

Plan 0002 renamed the roles: MODEL_UNBIASED is now the primary.
"""
import logging

import pytest

from config import Config
import venice_knowledge as vk
from venice_api import _http_error_hint


def _all_ids():
    return list(Config.configured_models().values())


# ── Pure detection ────────────────────────────────────────────────

def test_configured_models_cover_every_role():
    roles = Config.configured_models()
    assert set(roles) == {
        "MODEL_UNBIASED", "MODEL_COMPLEX", "MODEL_FALLBACK", "MODEL_LAST_RESORT", "MODEL_ROUTER",
    }
    assert roles["MODEL_UNBIASED"] == "deepseek-v4-1-flash-1017-uncensored"
    assert roles["MODEL_COMPLEX"] == "kimi-k3"
    assert roles["MODEL_ROUTER"] == "jev-latest"


def test_missing_configured_models_flags_absent_ids():
    available = [mid for mid in _all_ids() if mid != Config.MODEL_LAST_RESORT] + ["some-other-model"]
    missing = vk.missing_configured_models(available)
    assert missing == {"MODEL_LAST_RESORT": Config.MODEL_LAST_RESORT}


def test_missing_configured_models_empty_when_all_present():
    assert vk.missing_configured_models(_all_ids()) == {}


def test_committed_snapshot_lists_every_configured_model():
    snapshot_ids = [m.get("id") for m in vk._load_models_snapshot()]
    assert vk.missing_configured_models(snapshot_ids) == {}


# ── Startup validator ─────────────────────────────────────────────

def test_validate_logs_critical_when_primary_missing(monkeypatch, caplog):
    monkeypatch.setattr(
        vk, "get_models",
        lambda: [{"id": mid} for mid in _all_ids() if mid != Config.MODEL_UNBIASED],
    )
    with caplog.at_level(logging.CRITICAL, logger="venice_knowledge"):
        missing = vk.validate_configured_models()

    assert vk.PRIMARY_ROLE in missing
    assert any(r.levelno >= logging.CRITICAL for r in caplog.records)
    assert any(Config.MODEL_UNBIASED in r.getMessage() for r in caplog.records)


def test_validate_logs_error_not_critical_for_a_fallback(monkeypatch, caplog):
    monkeypatch.setattr(
        vk, "get_models",
        lambda: [{"id": mid} for mid in _all_ids() if mid != Config.MODEL_ROUTER],
    )
    with caplog.at_level(logging.ERROR, logger="venice_knowledge"):
        missing = vk.validate_configured_models()
    assert missing == {"MODEL_ROUTER": Config.MODEL_ROUTER}
    assert not any(r.levelno >= logging.CRITICAL for r in caplog.records)


def test_validate_returns_empty_when_all_present(monkeypatch):
    monkeypatch.setattr(vk, "get_models", lambda: [{"id": mid} for mid in _all_ids()])
    assert vk.validate_configured_models() == {}


def test_validate_skips_when_model_list_undetermined(monkeypatch, caplog):
    # Empty available list → can't tell; must NOT report everything missing.
    monkeypatch.setattr(vk, "get_models", lambda: [])
    with caplog.at_level(logging.WARNING, logger="venice_knowledge"):
        missing = vk.validate_configured_models()
    assert missing == {}
    assert any("skipping" in r.getMessage().lower() for r in caplog.records)


# ── HTTP error classification (part 2) ────────────────────────────

@pytest.mark.parametrize(
    "status, needle",
    [
        (401, "auth"),
        (402, "payment"),
        (404, "not found"),
        (429, "rate"),
    ],
)
def test_http_error_hint_known_codes(status, needle):
    hint = _http_error_hint(status)
    assert hint and needle in hint.lower()


def test_http_error_hint_unknown_code_is_none():
    assert _http_error_hint(500) is None
