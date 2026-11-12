"""Plan 0002 — decision-model lane routing (offline; Venice is stubbed)."""
from types import SimpleNamespace

import pytest
import requests

from config import Config
import router
from router import COMPLEX, UNBIASED, choose_lane


@pytest.mark.parametrize(
    "charged, filtered, complex_, lane",
    [
        (0.95, 0.14, 0.65, UNBIASED),   # contested claim
        (0.01, 0.02, 0.94, COMPLEX),    # math
        (0.17, 0.98, 0.92, UNBIASED),   # complex but filtered (drug dosing) stays uncensored
        (0.02, 0.78, 0.81, UNBIASED),   # lock picking
        (0.04, 0.03, 0.70, UNBIASED),   # under the complex threshold
        (0.50, 0.00, 0.99, UNBIASED),   # exactly at the charged threshold
        (0.00, 0.50, 0.99, UNBIASED),   # exactly at the filtered threshold
        (0.49, 0.49, 0.85, COMPLEX),    # exactly at the complex threshold
        (None, 0.0, 0.90, UNBIASED),    # missing answer
    ],
)
def test_choose_lane(charged, filtered, complex_, lane):
    assert choose_lane(charged, filtered, complex_) == lane


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


def _answers(charged, filtered, complex_, private_info=0.01):
    return {
        "charged": {"type": "noul", "noul": charged},
        "filtered": {"type": "noul", "noul": filtered},
        "complex": {"type": "noul", "noul": complex_},
        "private_info": {"type": "noul", "noul": private_info},
    }


def test_route_sends_four_questions_to_clef(monkeypatch):
    calls = []
    monkeypatch.setattr(router.requests, "post", _fake_post(_answers(0.05, 0.02, 0.9), calls=calls))
    r = router.route("integral of x^2 e^x", "X THREAD …", has_image=True)
    assert r.lane == COMPLEX and r.complex == 0.9 and r.filtered == 0.02
    sent = calls[0]
    assert sent["url"] == Config.VENICE_DECISIONS_URL
    assert sent["json"]["model"] == "clef-api"
    assert set(sent["json"]["questions"]) == {"charged", "filtered", "complex", "private_info"}
    assert not r.decline
    assert all(q["type"] == "noul" for q in sent["json"]["questions"].values())
    assert sent["json"]["state"] == {"question": "integral of x^2 e^x", "thread": "X THREAD …", "has_image": True}
    assert sent["timeout"] == Config.ROUTER_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "post",
    [
        _fake_post(status=403),
        _fake_post(exc=requests.exceptions.Timeout("slow")),
        _fake_post({"charged": {"type": "noul", "noul": 0.1}}),   # malformed answer
    ],
)
def test_route_raises_clef_unavailable_on_any_failure(monkeypatch, post):
    # An unrouted mention skips the doxxing decline, so it waits for the next poll instead.
    monkeypatch.setattr(router.requests, "post", post)
    with pytest.raises(router.ClefUnavailable, match="router"):
        router.route("anything")


def test_route_disabled_skips_the_call(monkeypatch):
    calls = []
    monkeypatch.setattr(router.requests, "post", _fake_post(_answers(0, 0, 1), calls=calls))
    monkeypatch.setattr(Config, "ROUTER_ENABLED", False)
    assert router.route("anything").lane == UNBIASED
    assert calls == []


def test_state_keeps_the_end_of_a_long_thread():
    thread = "old " * 5000 + "THE MENTION"
    state = router.build_state("q", thread, False)
    assert len(state["thread"]) == Config.ROUTER_STATE_MAX_CHARS
    assert state["thread"].endswith("THE MENTION")


def test_dox_requests_are_flagged_for_a_decline(monkeypatch):
    monkeypatch.setattr(router.requests, "post", _fake_post(_answers(0.1, 0.3, 0.1, private_info=0.98)))
    assert router.route("find this guy's home address").decline
