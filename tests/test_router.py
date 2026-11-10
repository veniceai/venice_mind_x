"""Plan 0002 — Jev lane routing (offline; Venice is stubbed)."""
from types import SimpleNamespace

import pytest
import requests

from config import Config
import router
from router import COMPLEX, UNBIASED, choose_lane


@pytest.mark.parametrize(
    "charged, complex_, lane",
    [
        (0.93, 0.78, UNBIASED),   # charged and complex: unbiased wins
        (0.02, 0.85, COMPLEX),    # math
        (0.03, 0.54, UNBIASED),   # under the complex threshold
        (0.10, 0.13, UNBIASED),   # simple fact
        (0.50, 0.99, UNBIASED),   # exactly at the charged threshold
        (0.49, 0.60, COMPLEX),    # exactly at the complex threshold
        (None, 0.90, UNBIASED),   # missing answer
    ],
)
def test_choose_lane(charged, complex_, lane):
    assert choose_lane(charged, complex_) == lane


def _fake_post(answers=None, *, status=200, exc=None, calls=None):
    def post(url, json=None, headers=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "json": json, "timeout": timeout})
        if exc:
            raise exc

        def raise_for_status():
            if status >= 400:
                raise requests.exceptions.HTTPError(f"HTTP {status}")

        return SimpleNamespace(raise_for_status=raise_for_status, json=lambda: {"answers": answers})
    return post


def _answers(charged, complex_):
    return {
        "charged": {"type": "noul", "noul": charged},
        "complex": {"type": "noul", "noul": complex_},
    }


def test_route_sends_both_questions_to_jev(monkeypatch):
    calls = []
    monkeypatch.setattr(router.requests, "post", _fake_post(_answers(0.05, 0.9), calls=calls))
    r = router.route("integral of x^2 e^x", "X THREAD …", has_image=True)
    assert r.lane == COMPLEX and r.complex == 0.9
    sent = calls[0]
    assert sent["url"] == Config.VENICE_DECISIONS_URL
    assert sent["json"]["model"] == "jev-latest"
    assert set(sent["json"]["questions"]) == {"charged", "complex"}
    assert all(q["type"] == "noul" for q in sent["json"]["questions"].values())
    assert sent["json"]["state"] == {"question": "integral of x^2 e^x", "thread": "X THREAD …", "has_image": True}
    assert sent["timeout"] == Config.ROUTER_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "post",
    [
        _fake_post(status=403),                                  # key limited to private models
        _fake_post(exc=requests.exceptions.Timeout("slow")),
        _fake_post({"charged": {"type": "noul"}}),               # malformed answer
    ],
)
def test_route_falls_back_to_unbiased_on_any_failure(monkeypatch, post):
    monkeypatch.setattr(router.requests, "post", post)
    r = router.route("anything")
    assert r.lane == UNBIASED
    assert r.reason.startswith("router error")


def test_route_disabled_skips_the_call(monkeypatch):
    calls = []
    monkeypatch.setattr(router.requests, "post", _fake_post(_answers(0, 1), calls=calls))
    monkeypatch.setattr(Config, "ROUTER_ENABLED", False)
    assert router.route("anything").lane == UNBIASED
    assert calls == []


def test_state_keeps_the_end_of_a_long_thread():
    thread = "old " * 5000 + "THE MENTION"
    state = router.build_state("q", thread, False)
    assert len(state["thread"]) == Config.ROUTER_STATE_MAX_CHARS
    assert state["thread"].endswith("THE MENTION")
