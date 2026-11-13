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


CLEAN = {k: 0.02 for k in reply_gate.QUESTIONS}


def test_gate_passes_a_clean_reply(monkeypatch):
    calls = []
    monkeypatch.setattr(reply_gate.requests, "post", _gate_post(CLEAN, calls=calls))
    assert reply_gate.crossed_line("Psilocybin's usual dose is 1-3 g dried.") is None
    assert calls[0]["model"] == Config.MODEL_ROUTER
    assert calls[0]["state"] == {"reply": "Psilocybin's usual dose is 1-3 g dried.", "thread": "", "sources": ""}


def test_gate_sees_thread_and_sources(monkeypatch):
    calls = []
    monkeypatch.setattr(reply_gate.requests, "post", _gate_post(CLEAN, calls=calls))
    reply_gate.crossed_line("Yes.", thread="X THREAD", sources="[web] Reuters")
    assert calls[0]["state"]["thread"] == "X THREAD"
    assert calls[0]["state"]["sources"] == "[web] Reuters"
    assert {"unsupported", "speculation", "token_returns"} <= set(calls[0]["questions"])


def test_gate_blocks_a_crossed_line(monkeypatch):
    monkeypatch.setattr(reply_gate.requests, "post", _gate_post({**CLEAN, "private_info": 0.93}))
    assert reply_gate.crossed_line("He lives at 12 Elm St, 555-0100") == "private_info"


def test_gate_raises_clef_unavailable_when_it_cannot_run(monkeypatch):
    from router import ClefUnavailable
    monkeypatch.setattr(reply_gate.requests, "post", _gate_post(exc=requests.exceptions.Timeout("slow")))
    with pytest.raises(ClefUnavailable, match="gate"):
        reply_gate.crossed_line("anything")


def test_gate_can_be_turned_off(monkeypatch):
    calls = []
    monkeypatch.setattr(reply_gate.requests, "post", _gate_post(CLEAN, calls=calls))
    monkeypatch.setattr(Config, "REPLY_GATE_ENABLED", False)
    assert reply_gate.crossed_line("anything") is None
    assert calls == []


@pytest.mark.parametrize(
    "text, out",
    [
        ("Read it here https://example.com/report?x=1 before you share.", "Read it here before you share."),
        ("Source: reuters.com/world/fed-holds-2026-10-28", "Source:"),
        ("Only venice.ai is official; vvvevent.com/claim is a scam.", "Only venice.ai is official; is a scam."),
        ("Typical dose is 2-3.5g, and/or 10-25 mg.", "Typical dose is 2-3.5g, and/or 10-25 mg."),
    ],
)
def test_links_are_stripped_bare_domains_stay(text, out):
    assert fit_x_rules(text) == out


@pytest.mark.parametrize(
    "text, flagged",
    [
        ("DIEM: ~$1,718 for $1/day of API credit, ~21% implied yield.", True),
        ("Staking sVVV pays an APR you can see on the dashboard.", True),
        ("VVV is down 38% from its September high.", True),
        ("DIEM gives $1/day of Venice API credit per staked token.", False),
        ("Bitcoin is up 12% this month.", False),
        ("VVV is Venice's token on Base; stake it to mint DIEM.", False),
    ],
)
def test_token_returns_backstop(text, flagged):
    from safety import mentions_token_returns
    assert mentions_token_returns(text) is flagged
