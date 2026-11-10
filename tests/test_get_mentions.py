"""Finding E — don't miss mentions during bursts.

get_mentions previously requested only 5 mentions (the API per-page minimum), so
a spike of activity left mentions unseen. It now fetches up to
Config.MAX_MENTIONS_PER_CHECK across as many pages as needed, merging each page's
data and de-duplicating the included media/users.
"""
from types import SimpleNamespace

import pytest

from config import Config
from twitter_client import get_mentions


class _Tweet:
    def __init__(self, tid): self.id = tid

class _User:
    def __init__(self, uid): self.id = uid

class _Media:
    def __init__(self, key): self.media_key = key


class FakeClient:
    """Serves predefined pages; records each call's kwargs.

    pages: list of {data, users, media, next_token}. Page index is derived from
    the pagination_token ("1" -> pages[1]); None token -> pages[0].
    """
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_users_mentions(self, **kwargs):
        self.calls.append(kwargs)
        token = kwargs.get("pagination_token")
        idx = 0 if token is None else int(token)
        page = self.pages[idx]
        meta = {"next_token": page["next_token"]} if page.get("next_token") else {}
        return SimpleNamespace(
            data=page.get("data"),
            includes={"media": page.get("media", []), "users": page.get("users", [])},
            meta=meta,
        )


def test_single_page_returns_all_and_merges_includes():
    client = FakeClient([
        {"data": [_Tweet(1), _Tweet(2)], "users": [_User(10)], "next_token": None},
    ])
    resp = get_mentions(client, 999, max_results=50)
    assert [t.id for t in resp.data] == [1, 2]
    assert {u.id for u in resp.includes["users"]} == {10}
    assert len(client.calls) == 1
    assert client.calls[0]["id"] == 999


def test_paginates_and_dedups_includes():
    client = FakeClient([
        {"data": [_Tweet(1)], "users": [_User(10)], "media": [_Media("m1")], "next_token": "1"},
        {"data": [_Tweet(2)], "users": [_User(10), _User(11)], "media": [_Media("m2")], "next_token": None},
    ])
    resp = get_mentions(client, 999, max_results=50)
    assert [t.id for t in resp.data] == [1, 2]
    assert {u.id for u in resp.includes["users"]} == {10, 11}        # deduped
    assert {m.media_key for m in resp.includes["media"]} == {"m1", "m2"}
    assert len(client.calls) == 2
    assert client.calls[1]["pagination_token"] == "1"               # used next_token


def test_respects_total_cap_across_pages():
    client = FakeClient([
        {"data": [_Tweet(1), _Tweet(2)], "next_token": "1"},
        {"data": [_Tweet(3), _Tweet(4)], "next_token": "2"},
        {"data": [_Tweet(5), _Tweet(6)], "next_token": None},
    ])
    resp = get_mentions(client, 999, max_results=3)
    assert len(resp.data) == 3
    assert [t.id for t in resp.data] == [1, 2, 3]


def test_stops_when_page_empty():
    client = FakeClient([{"data": None, "next_token": None}])
    resp = get_mentions(client, 999, max_results=50)
    assert resp.data == []
    assert len(client.calls) == 1


def test_uses_user_context_for_owned_read_pricing():
    client = FakeClient([{"data": [_Tweet(1)], "next_token": None}])
    get_mentions(client, 999, max_results=50)
    assert client.calls[0]["user_auth"] is True


def test_passes_start_time_through():
    client = FakeClient([{"data": [_Tweet(1)], "next_token": None}])
    get_mentions(client, 999, start_time="2026-01-01T00:00:00.000Z", max_results=50)
    assert client.calls[0]["start_time"] == "2026-01-01T00:00:00.000Z"


def test_default_cap_is_more_than_legacy_five():
    # Regression guard: the whole point of finding E is to stop capping at 5.
    assert Config.MAX_MENTIONS_PER_CHECK > 5
