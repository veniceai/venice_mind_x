"""tweepy-shaped fakes for the X context and X tool tests (no network)."""
from types import SimpleNamespace

import tweepy


def user(uid, username, name=None, **extra):
    return SimpleNamespace(id=uid, username=username, name=name, **extra)


def media(key, type_="photo", url=None, preview=None):
    return SimpleNamespace(media_key=key, type=type_, url=url, preview_image_url=preview)


def tweet(tid, text, author_id, *, reply_to=None, quote_of=None, conversation_id=None,
          media_keys=(), urls=(), note=None, created_at="2026-11-09T14:00:00.000Z"):
    refs = []
    if reply_to:
        refs.append(SimpleNamespace(type="replied_to", id=reply_to))
    if quote_of:
        refs.append(SimpleNamespace(type="quoted", id=quote_of))
    raw = {"id": str(tid), "text": text}
    if note:
        raw["note_tweet"] = {"text": note}
    return SimpleNamespace(
        id=tid, text=text, author_id=author_id, created_at=created_at,
        conversation_id=conversation_id or tid,
        referenced_tweets=refs or None,
        attachments={"media_keys": list(media_keys)} if media_keys else None,
        entities={"urls": [{"expanded_url": u} for u in urls]} if urls else None,
        data=raw,
    )


def resp(data, *, users=(), media_=(), tweets=()):
    return SimpleNamespace(
        data=data,
        includes={"users": list(users), "media": list(media_), "tweets": list(tweets)},
        meta={},
    )


def _error(cls, status):
    return cls(SimpleNamespace(status_code=status, reason="", json=lambda: {}, text=""))


def too_many_requests():
    return _error(tweepy.errors.TooManyRequests, 429)


def forbidden():
    return _error(tweepy.errors.Forbidden, 403)


class FakeCache:
    """Stands in for TweetCache: id → response, or an exception to raise."""

    def __init__(self, posts=None):
        self.posts = {str(k): v for k, v in (posts or {}).items()}
        self.calls = []

    def get(self, tweet_id):
        self.calls.append(str(tweet_id))
        item = self.posts.get(str(tweet_id))
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    """Records the X API calls the tools make; each returns a canned response."""

    def __init__(self, *, search=None, user=None, user_posts=None):
        self.search, self.user, self.user_posts = search, user, user_posts
        self.calls = []

    def _answer(self, item):
        if isinstance(item, Exception):
            raise item
        return item

    def search_recent_tweets(self, **kw):
        self.calls.append(("search_recent_tweets", kw))
        return self._answer(self.search)

    def get_user(self, **kw):
        self.calls.append(("get_user", kw))
        return self._answer(self.user)

    def get_users_tweets(self, user_id, **kw):
        self.calls.append(("get_users_tweets", user_id, kw))
        return self._answer(self.user_posts)


def fake_downloader(url):
    return (b"\xff\xd8fakejpeg", "image/jpeg")
