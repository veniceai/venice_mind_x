"""The reply log: one JSON line per handled mention, so an incident can be traced
to the route, the models and the gate. Offline; Venice and X are stubbed."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

import bot
import venice_api
from config import Config
from reply_log import log_reply
from router import COMPLEX, Route
from state import State


# ── log_reply ────────────────────────────────────────────────────

def test_appends_one_json_line_per_record(tmp_path):
    path = tmp_path / "logs" / "replies.jsonl"
    log_reply({"tweet_id": "1", "reply": "Yes.", "posted": True}, path=str(path))
    log_reply({"tweet_id": "2", "reply": "No.", "posted": False}, path=str(path))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["tweet_id"] == "1" and first["posted"] is True and "ts" in first


def test_keeps_non_ascii_text_readable(tmp_path):
    path = tmp_path / "replies.jsonl"
    log_reply({"reply": "Sim, é verdade."}, path=str(path))
    assert "é verdade" in path.read_text(encoding="utf-8")


def test_a_write_failure_does_not_raise(tmp_path):
    blocked = tmp_path / "replies.jsonl"
    blocked.mkdir()  # a directory where the file should be makes open() fail
    log_reply({"reply": "x"}, path=str(blocked))


# ── The trace venice_api keeps for each mention ──────────────────

class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def test_calls_record_model_citations_and_cost(monkeypatch):
    body = {
        "choices": [{"message": {"content": "ok"}}],
        "venice_parameters": {"web_search_citations": [{"url": "a"}, {"url": "b"}]},
        "cost": {"usd": 0.004, "diem": 0},
    }
    monkeypatch.setattr(venice_api.requests, "post", lambda *a, **k: _Resp(body))
    monkeypatch.setattr(venice_api, "_supports", lambda *a: False)
    venice_api.reset_trace()
    venice_api._call_venice("deepseek", "sys", "q")
    assert venice_api.get_trace()["calls"] == [{"model": "deepseek", "citations": 2, "cost_usd": 0.004}]


def test_failed_calls_are_recorded(monkeypatch):
    def down(*a, **k):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(venice_api.requests, "post", down)
    monkeypatch.setattr(venice_api, "_supports", lambda *a: False)
    venice_api.reset_trace()
    assert venice_api._call_venice("deepseek", "sys", "q") is None
    (call,) = venice_api.get_trace()["calls"]
    assert call["model"] == "deepseek" and "connection refused" in call["error"]


def test_analyse_records_the_route(monkeypatch):
    monkeypatch.setattr(venice_api, "route", lambda *a, **k: Route(
        COMPLEX, charged=0.1, filtered=0.2, complex=0.9, private_info=0.0))
    monkeypatch.setattr(venice_api, "_call_venice", lambda *a, **k: "[FINAL_REPLY]ok[/FINAL_REPLY]")
    venice_api.analyse("integral of x squared?")
    route = venice_api.get_trace()["route"]
    assert route["lane"] == COMPLEX and route["complex"] == 0.9 and route["charged"] == 0.1


# ── The bot writes one record per handled mention ────────────────

AUTHOR = SimpleNamespace(id=2, protected=False, username="someone", verified=False)
REPLY = "Yes. Two outlets reported it."


@pytest.fixture
def logged(monkeypatch):
    records = []
    monkeypatch.setattr(bot, "log_reply", records.append)
    ctx = SimpleNamespace(
        posts=[object()], linked=[], images=[], rate_limited=False, urls=[], permalinks=[],
        render=lambda: "X THREAD", participants=lambda: [],
    )
    monkeypatch.setattr(bot, "build_context", lambda *a, **k: ctx)
    monkeypatch.setattr(bot, "question_text", lambda ctx, handle: "is this true?")
    box = SimpleNamespace(calls=2, posts_fetched=5, evidence=["[X] a post"], permalinks=[])
    monkeypatch.setattr(bot, "XToolbox", lambda *a, **k: box)
    monkeypatch.setattr(bot, "analyse", lambda *a, **k: f"[FINAL_REPLY]{REPLY}[/FINAL_REPLY]")
    monkeypatch.setattr(bot, "craft_tweet", lambda *a, **k: REPLY)
    monkeypatch.setattr(bot, "get_trace", lambda: {
        "route": {"lane": "unbiased"},
        "calls": [{"model": "deepseek", "citations": 3, "cost_usd": 0.01}],
    })
    return records


def _process_one_mention():
    instance = object.__new__(bot.VeniceBot)
    instance.state = State()
    instance.bot_id = 999
    instance.bot_username = "venice_mind"
    instance.tweet_cache = None
    instance.client = None
    instance.hourly_replies = 0
    instance.user_reply_counts = {}
    instance.clef_attempts = {}
    mention = SimpleNamespace(id=1, author_id=2, conversation_id=1, created_at=datetime.now(timezone.utc))
    instance._process_tweet(mention, includes={}, user_lookup={2: AUTHOR})


def test_a_dry_run_reply_is_logged_with_route_and_models(monkeypatch, logged):
    monkeypatch.setattr(Config, "DRY_RUN", True)
    monkeypatch.setattr(bot, "finalize_reply", lambda text, **k: (text, None))
    monkeypatch.setattr(bot, "reply_to_tweet", lambda *a, **k: pytest.fail("a dry run must not post"))
    _process_one_mention()

    (record,) = logged
    assert record["posted"] is False and record["dry_run"] is True
    assert record["reply"] == REPLY and record["chars"] == len(REPLY)
    assert record["route"] == {"lane": "unbiased"}
    assert record["calls"][0] == {"model": "deepseek", "citations": 3, "cost_usd": 0.01}
    assert record["tool_calls"] == 2 and record["sources"] == 1 and record["author"] == "someone"


def test_a_reply_the_gate_stops_is_logged_with_its_draft(monkeypatch, logged):
    monkeypatch.setattr(Config, "DRY_RUN", False)
    monkeypatch.setattr(bot, "finalize_reply", lambda text, **k: (None, "unsupported"))
    monkeypatch.setattr(bot, "reply_to_tweet", lambda *a, **k: pytest.fail("a stopped reply must not post"))
    _process_one_mention()

    (record,) = logged
    assert record["posted"] is False
    assert record["skipped"] == "failed_unsupported_check"
    assert record["draft"] == REPLY


def test_a_posted_reply_is_logged_with_its_id(monkeypatch, logged):
    monkeypatch.setattr(Config, "DRY_RUN", False)
    monkeypatch.setattr(bot, "finalize_reply", lambda text, **k: (text, None))
    monkeypatch.setattr(bot, "reply_to_tweet", lambda client, tweet_id, text: SimpleNamespace(data={"id": "555"}))
    monkeypatch.setattr(bot.time, "sleep", lambda seconds: None)
    _process_one_mention()

    (record,) = logged
    assert record["posted"] is True and record["reply_id"] == "555" and record["reply"] == REPLY


def test_a_failed_analysis_is_logged(monkeypatch, logged):
    monkeypatch.setattr(bot, "analyse", lambda *a, **k: Config.ERROR_MESSAGE)
    _process_one_mention()

    (record,) = logged
    assert record["skipped"] == "analysis_failed" and record["posted"] is False
