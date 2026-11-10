"""X's self-serve posting rules and the hard-line reply gate (offline)."""
from types import SimpleNamespace

import pytest
import requests

from config import Config
import reply_gate
from twitter_client import fit_x_rules
from x_context import build_context
from x_fakes import FakeCache, resp, tweet, user


@pytest.mark.parametrize(
    "text, out",
    [
        ("$VVV and $DIEM both moved", "$VVV and DIEM both moved"),
        ("Stake $VVV, mint $DIEM, compare $BTC.", "Stake $VVV, mint DIEM, compare BTC."),
        ("It costs $20, not $VVV", "It costs $20, not $VVV"),           # dollar amounts aren't cashtags
        ("one $ETH only", "one $ETH only"),
    ],
)
def test_one_cashtag_per_post(text, out):
    assert fit_x_rules(text) == out


def test_mentions_outside_the_conversation_lose_their_at():
    text = "@bob is right; @elonmusk said otherwise, email me@example.com"
    assert fit_x_rules(text, ["bob"]) == "@bob is right; elonmusk said otherwise, email me@example.com"


def test_participants_exclude_the_asker_and_the_bot():
    cache = FakeCache({
        100: resp(tweet(100, "root", 1), users=[user(1, "alice")]),
        101: resp(tweet(101, "mine", 999, reply_to=100, conversation_id=100), users=[user(999, "venice_mind")]),
    })
    mention = tweet(102, "@venice_mind ?", 3, reply_to=101, conversation_id=100)
    ctx = build_context(cache, mention, {"users": [user(3, "carol")]}, bot_id=999)
    assert ctx.participants() == ["alice"]


def _gate_post(scores=None, exc=None, calls=None):
    def post(url, json=None, headers=None, timeout=None):
        if calls is not None:
            calls.append(json)
        if exc:
            raise exc
        answers = {k: {"type": "noul", "noul": v} for k, v in (scores or {}).items()}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"answers": answers})
    return post


CLEAN = {"minors": 0.01, "private_info": 0.02, "threat": 0.01, "mass_harm": 0.01}


def test_gate_passes_a_clean_reply(monkeypatch):
    calls = []
    monkeypatch.setattr(reply_gate.requests, "post", _gate_post(CLEAN, calls=calls))
    assert reply_gate.crossed_line("Psilocybin's usual dose is 1-3 g dried.") is None
    assert calls[0]["model"] == Config.MODEL_ROUTER
    assert calls[0]["state"] == {"reply": "Psilocybin's usual dose is 1-3 g dried."}


def test_gate_blocks_a_crossed_line(monkeypatch):
    monkeypatch.setattr(reply_gate.requests, "post", _gate_post({**CLEAN, "private_info": 0.93}))
    assert reply_gate.crossed_line("He lives at 12 Elm St, 555-0100") == "private_info"


def test_gate_fails_open(monkeypatch):
    monkeypatch.setattr(reply_gate.requests, "post", _gate_post(exc=requests.exceptions.Timeout("slow")))
    assert reply_gate.crossed_line("anything") is None


def test_gate_can_be_turned_off(monkeypatch):
    calls = []
    monkeypatch.setattr(reply_gate.requests, "post", _gate_post(CLEAN, calls=calls))
    monkeypatch.setattr(Config, "REPLY_GATE_ENABLED", False)
    assert reply_gate.crossed_line("anything") is None
    assert calls == []
