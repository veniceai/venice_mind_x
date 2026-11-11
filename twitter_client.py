"""Twitter API wrappers — mentions, replies, tweet lookup."""
import logging
import re
from types import SimpleNamespace

import tweepy
from config import Config

logger = logging.getLogger(__name__)

# note_tweet carries the full text of posts over 280 characters; `text` is
# only the truncated preview for those.
_TWEET_FIELDS = [
    "created_at", "author_id", "attachments", "public_metrics",
    "conversation_id", "referenced_tweets", "in_reply_to_user_id", "entities",
    "note_tweet", "lang",
]
_EXPANSIONS = [
    "attachments.media_keys", "author_id",
    "in_reply_to_user_id", "referenced_tweets.id", "referenced_tweets.id.author_id",
]
_MEDIA_FIELDS = ["type", "url", "preview_image_url", "alt_text"]
_USER_FIELDS = ["protected", "verified", "username", "name"]
_PROFILE_FIELDS = ["username", "name", "description", "verified", "public_metrics", "created_at"]


def get_mentions(
    client: tweepy.Client, bot_user_id: int, *, start_time: str = None, max_results: int = None
):
    """Fetch up to `max_results` mentions, paginating as needed.

    The mentions endpoint returns at most 100 per page (minimum 5). Requesting
    only the minimum (the old behaviour) meant bursts of activity left mentions
    unseen. This walks the pages until the cap is reached or there are no more,
    merging each page's tweets and de-duplicating the included media/users.

    Returns an object with `.data` (list) and `.includes` (dict), matching how
    the bot consumes a single-page response.
    """
    max_total = max_results or Config.MAX_MENTIONS_PER_CHECK

    data = []
    media, users, tweets = [], [], []
    seen_media, seen_users, seen_tweets = set(), set(), set()
    token = None

    while len(data) < max_total:
        page_size = min(100, max(5, max_total - len(data)))
        kwargs = {
            "id": bot_user_id,
            "max_results": page_size,
            "tweet_fields": _TWEET_FIELDS,
            "expansions": _EXPANSIONS,
            "media_fields": _MEDIA_FIELDS,
            "user_fields": _USER_FIELDS,
            # User context, not the app bearer token: X bills an account's own
            # mentions as Owned Reads ($0.001 vs $0.005 per post) only then.
            "user_auth": True,
        }
        if start_time:
            kwargs["start_time"] = start_time
        if token:
            kwargs["pagination_token"] = token

        resp = client.get_users_mentions(**kwargs)
        if not resp or not resp.data:
            break

        data.extend(resp.data)

        includes = resp.includes or {}
        for m in includes.get("media", []):
            if m.media_key not in seen_media:
                seen_media.add(m.media_key)
                media.append(m)
        for u in includes.get("users", []):
            if u.id not in seen_users:
                seen_users.add(u.id)
                users.append(u)
        for t in includes.get("tweets", []):
            if t.id not in seen_tweets:
                seen_tweets.add(t.id)
                tweets.append(t)

        token = (resp.meta or {}).get("next_token")
        if not token:
            break

    return SimpleNamespace(
        data=data[:max_total],
        includes={"media": media, "users": users, "tweets": tweets},
    )


# twitter-text's cashtag shape: $ + 1-6 letters, optional ._ suffix.
_CASHTAG_RE = re.compile(r"(?<![\w$])\$([A-Za-z]{1,6}(?:[._][A-Za-z]{1,2})?)(?![\w])")
_HANDLE_RE = re.compile(r"(?<![\w@])@(\w{1,15})\b")
# Links with a scheme, and schemeless links with a path. Bare domains such as
# venice.ai (in the scam warnings) stay.
_LINK_RE = re.compile(
    r"https?://\S+|(?<![@\w.])(?:[a-z0-9-]+\.)+[a-z]{2,}/[^\s'\"()]*", re.IGNORECASE
)


def fit_x_rules(text: str, participants=()) -> str:
    """Make a reply acceptable to X's self-serve posting rules, and cheap.

    X rejects API posts with more than one cashtag, and only lets API posts
    @mention accounts already in the conversation. Extra cashtags lose their $;
    @handles outside `participants` lose their @. Links are removed: a post
    with a URL costs $0.20 against $0.01 for a summoned reply.
    """
    text = re.sub(r"[ \t]{2,}", " ", _LINK_RE.sub("", text)).strip()
    seen = {"n": 0}

    def cashtag(m):
        seen["n"] += 1
        return m.group(0) if seen["n"] == 1 else m.group(1)

    allowed = {p.lower() for p in participants if p}
    text = _CASHTAG_RE.sub(cashtag, text)
    return _HANDLE_RE.sub(lambda m: m.group(0) if m.group(1).lower() in allowed else m.group(1), text)


def reply_to_tweet(client: tweepy.Client, tweet_id: int, text: str):
    char_limit = Config.char_limit()

    if len(text) <= char_limit:
        try:
            resp = client.create_tweet(text=text, in_reply_to_tweet_id=tweet_id)
            logger.info(f"Replied to {tweet_id} → {resp.data['id']}")
            return resp
        except tweepy.errors.Forbidden as e:
            logger.error(f"403 on reply to {tweet_id}: {e}")
            return None

    # Truncate gracefully
    truncated = text[: char_limit - 10]
    for punct in (". ", "! ", "? "):
        idx = truncated.rfind(punct)
        if idx > char_limit * 0.7:
            truncated = text[: idx + 1]
            break
    else:
        truncated = text[: char_limit - 3] + "…"

    try:
        resp = client.create_tweet(text=truncated, in_reply_to_tweet_id=tweet_id)
        logger.info(f"Replied (truncated) to {tweet_id} → {resp.data['id']}")
        return resp
    except tweepy.errors.Forbidden as e:
        logger.error(f"403 on truncated reply to {tweet_id}: {e}")
        return None


def get_tweet_by_id(client: tweepy.Client, tweet_id: int):
    return client.get_tweet(
        tweet_id,
        tweet_fields=_TWEET_FIELDS,
        expansions=_EXPANSIONS,
        media_fields=_MEDIA_FIELDS,
        user_fields=_USER_FIELDS,
    )


def search_recent(client: tweepy.Client, query: str, *, latest: bool = False, max_results: int = None):
    """Recent search (last 7 days). Needs recent-search access on the X API plan."""
    return client.search_recent_tweets(
        query=query,
        max_results=max(10, min(100, max_results or Config.X_SEARCH_MAX_RESULTS)),
        sort_order="recency" if latest else "relevancy",
        tweet_fields=_TWEET_FIELDS,
        expansions=_EXPANSIONS,
        media_fields=_MEDIA_FIELDS,
        user_fields=_USER_FIELDS,
    )


def get_user_by_username(client: tweepy.Client, username: str):
    return client.get_user(username=username, user_fields=_PROFILE_FIELDS)


def get_user_posts(client: tweepy.Client, user_id, max_results: int = None):
    return client.get_users_tweets(
        user_id,
        max_results=max(5, min(100, max_results or Config.X_USER_RECENT_POSTS)),
        exclude=["retweets"],
        tweet_fields=_TWEET_FIELDS,
        expansions=_EXPANSIONS,
        media_fields=_MEDIA_FIELDS,
        user_fields=_USER_FIELDS,
    )


class TweetCache:
    """Memoize get_tweet_by_id for the process lifetime.

    Parent/root/quoted tweets are immutable, so the bot needn't re-fetch the
    same tweet on every reply in a thread. Bounded (oldest evicted first) so it
    can't grow without limit. Only successful lookups are cached; None results
    (errors/missing) are left uncached so they get another chance later.
    """

    def __init__(self, client: tweepy.Client, max_size: int = None):
        self._client = client
        self._cache: dict = {}
        self._max = max_size or Config.TWEET_CACHE_SIZE
        self.hits = 0
        self.misses = 0

    def get(self, tweet_id):
        key = str(tweet_id)
        if key in self._cache:
            self.hits += 1
            return self._cache[key]

        self.misses += 1
        resp = get_tweet_by_id(self._client, tweet_id)
        if resp is not None:
            self._cache[key] = resp
            if len(self._cache) > self._max:
                self._cache.pop(next(iter(self._cache)))
        return resp
