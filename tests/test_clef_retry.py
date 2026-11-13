"""Clef outages: the mention is held for the next poll instead of posting unrouted
or unchecked, and the bot gives up after CLEF_MAX_ATTEMPTS tries. Offline."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import bot
from config import Config
from router import ClefUnavailable
from state import State

AUTHOR = SimpleNamespace(id=2, protected=False, username="someone", verified=False)
REPLY = "Yes. Two outlets reported it."


def _clef_down(*a, **k):
    raise ClefUnavailable("router: timeout")


@pytest.fixture
def logged(monkeypatch):
    records = []
    monkeypatch.setattr(Config, "DRY_RUN", False)
    monkeypatch.setattr(bot, "log_reply", records.append)
    ctx = SimpleNamespace(
        posts=[object()], linked=[], images=[], rate_limited=False, urls=[], permalinks=[],
        render=lambda: "X THREAD", participants=lambda: [],
    )
    monkeypatch.setattr(bot, "build_context", lambda *a, **k: ctx)
    monkeypatch.setattr(bot, "question_text", lambda ctx, handle: "is this true?")
    box = SimpleNamespace(calls=0, posts_fetched=0, evidence=[], permalinks=[])
    monkeypatch.setattr(bot, "XToolbox", lambda *a, **k: box)
    monkeypatch.setattr(bot, "analyse", lambda *a, **k: f"[FINAL_REPLY]{REPLY}[/FINAL_REPLY]")
    monkeypatch.setattr(bot, "craft_tweet", lambda *a, **k: REPLY)
    monkeypatch.setattr(bot, "finalize_reply", lambda text, **k: (text, None))
    monkeypatch.setattr(bot, "get_trace", lambda: {"route": None, "calls": []})
    monkeypatch.setattr(bot, "reply_to_tweet", lambda *a, **k: pytest.fail("nothing should post during an outage"))
    monkeypatch.setattr(bot.time, "sleep", lambda seconds: None)
    return records


def _bot():
    instance = object.__new__(bot.VeniceBot)
    instance.state = State()
    instance.bot_id = 999
    instance.bot_username = "venice_mind"
    instance.tweet_cache = None
    instance.client = None
    instance.hourly_replies = 0
    instance.user_reply_counts = {}
    instance.clef_attempts = {}
    return instance


def _mention():
    return SimpleNamespace(id=1, author_id=2, conversation_id=1, created_at=datetime.now(timezone.utc))


def test_a_router_outage_holds_the_mention_for_the_next_poll(monkeypatch, logged):
    monkeypatch.setattr(bot, "analyse", _clef_down)
    instance = _bot()
    instance._process_tweet(_mention(), includes={}, user_lookup={2: AUTHOR})

    assert not instance.state.is_processed(1)
    assert instance.clef_attempts == {"1": 1}
    (record,) = logged
    assert record["skipped"] == "clef_unavailable" and record["attempts"] == 1 and record["posted"] is False


def test_a_gate_outage_holds_the_reply_instead_of_posting_it_unchecked(monkeypatch, logged):
    monkeypatch.setattr(bot, "finalize_reply", _clef_down)
    instance = _bot()
    instance._process_tweet(_mention(), includes={}, user_lookup={2: AUTHOR})

    assert not instance.state.is_processed(1)
    assert logged[0]["skipped"] == "clef_unavailable"


def test_the_bot_gives_up_after_the_last_attempt(monkeypatch, logged):
    monkeypatch.setattr(bot, "analyse", _clef_down)
    instance = _bot()
    instance.clef_attempts = {"1": Config.CLEF_MAX_ATTEMPTS - 1}
    instance._process_tweet(_mention(), includes={}, user_lookup={2: AUTHOR})

    assert instance.state.is_processed(1)
    assert instance.clef_attempts == {}
    assert logged[0]["gave_up"] is True and logged[0]["attempts"] == Config.CLEF_MAX_ATTEMPTS


def test_a_mention_that_gets_through_after_a_blip_clears_its_count(monkeypatch, logged):
    posted = []
    monkeypatch.setattr(bot, "reply_to_tweet", lambda client, tweet_id, text: posted.append(text)
                        or SimpleNamespace(data={"id": "555"}))
    instance = _bot()
    instance.clef_attempts = {"1": 2}
    instance._process_tweet(_mention(), includes={}, user_lookup={2: AUTHOR})

    assert posted == [REPLY]
    assert instance.clef_attempts == {}
    assert instance.state.is_processed(1)
