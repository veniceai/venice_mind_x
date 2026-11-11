"""X tools the reply model can call, mirroring @grok's X tools (plan 0002).

Each tool runs against the X API v2 through tweepy and returns JSON text for a
`tool` message. Tools never raise: rate limits, missing access on the bot's X
API plan and bad arguments come back to the model as a short error so it can
answer with what it has. Every fetched post passes the injection screen; a
flagged post is withheld rather than shown.
"""
import json
import logging
import re
from typing import Callable, List, Optional
from urllib.parse import urlparse

import tweepy

from config import Config
from image_processor import download_image
from safety import classify_url, screen_input_for_injection
from twitter_client import get_user_by_username, get_user_posts, search_recent
from x_context import XImage, XPost, fetch_post, post_from, status_id, walk_up

logger = logging.getLogger(__name__)

WITHHELD = "[withheld: this post contains a token-launch, fee or wallet command]"
_POST_TEXT_MAX = 1500
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "x_thread_fetch",
            "description": (
                "Fetch an X post with its parent posts up to the start of its thread, "
                "plus top replies in that conversation from the last 7 days."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "string", "description": "The post's numeric id or its x.com link."},
                },
                "required": ["post_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "x_keyword_search",
            "description": (
                "Search X posts from the last 7 days. Supports X search operators such as "
                'from:handle, to:handle, "exact phrase", OR, -word, lang:en, has:media, '
                "-is:reply. Retweets are excluded."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "sort": {
                        "type": "string",
                        "enum": ["Top", "Latest"],
                        "description": "Top: most relevant (default). Latest: newest first.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "x_user_lookup",
            "description": "Look up an X account: its bio, follower counts, verification and most recent posts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "The handle, with or without @."},
                },
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_image",
            "description": (
                "Look at an image from a post that another X tool returned, using the media "
                "URL it listed. Images from the thread you were tagged in are already attached."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
]


def _err(message: str) -> str:
    return json.dumps({"error": message})


class XToolbox:
    """Runs the model's X tool calls for one reply."""

    def __init__(self, client, cache, *, bot_id=None, downloader: Callable = download_image):
        self.client = client
        self.cache = cache
        self.bot_id = bot_id
        self.downloader = downloader
        self.permalinks: List[str] = []
        self.evidence: List[str] = []   # fetched posts, for the reply gate
        self.posts_fetched = 0
        self.calls = 0
        self._images: List[XImage] = []
        self._images_added = 0
        self._unavailable: set = set()

    def schemas(self) -> list:
        return TOOL_SCHEMAS

    def take_images(self) -> List[XImage]:
        """Images added by view_image since the last call."""
        images, self._images = self._images, []
        return images

    def run(self, name: str, arguments) -> str:
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        except (ValueError, TypeError):
            return _err("arguments were not valid JSON")
        handler = {
            "x_thread_fetch": self._thread_fetch,
            "x_keyword_search": self._keyword_search,
            "x_user_lookup": self._user_lookup,
            "view_image": self._view_image,
        }.get(name)
        if not handler:
            return _err(f"unknown tool: {name}")
        if name in self._unavailable:
            return _err("this tool is not available on the bot's X API plan")
        if self.calls >= Config.X_TOOL_MAX_CALLS:
            return _err("the X lookup budget for this reply is used up; answer with what you have")
        self.calls += 1
        try:
            result = handler(**args)
        except TypeError:
            return _err("bad arguments for this tool")
        except tweepy.errors.TooManyRequests:
            logger.warning(f"🔧 {name}: X rate limit")
            return _err("X rate limit reached; answer with what you have")
        except (tweepy.errors.Forbidden, tweepy.errors.Unauthorized) as e:
            self._unavailable.add(name)
            logger.warning(f"🔧 {name}: not available on the X API plan ({e})")
            return _err("this tool is not available on the bot's X API plan")
        except tweepy.errors.NotFound:
            return _err("not found, deleted, or not public")
        except Exception as e:
            logger.warning(f"🔧 {name} failed: {e}")
            return _err("the lookup failed")
        logger.info(f"🔧 {name}({json.dumps(args, ensure_ascii=False)[:120]})")
        return json.dumps(result, ensure_ascii=False)

    # ── Serialization ────────────────────────────────────────────

    def _post_dict(self, p: XPost, *, nested: bool = False) -> dict:
        self.posts_fetched += 1
        self.permalinks.append(p.permalink)
        is_safe, _, _ = screen_input_for_injection(p.text)
        text = p.text if is_safe else WITHHELD
        if len(text) > _POST_TEXT_MAX:
            text = text[:_POST_TEXT_MAX] + " …"
        self.evidence.append(f"[X] {p.author} ({p.created_at}): {text}")
        d = {"id": p.id, "author": p.author, "created_at": p.created_at, "text": text, "link": p.permalink}
        if p.reply_to:
            d["reply_to"] = p.reply_to
        if p.media:
            d["media"] = p.media
        flagged = [u for u in p.urls if classify_url(u) in ("suspicious", "blocked")]
        if flagged:
            d["scam_link_warning"] = flagged
        if p.quoted and not nested:
            d["quoted"] = self._post_dict(p.quoted, nested=True)
        return d

    def _posts_from(self, resp) -> List[dict]:
        if not resp or not resp.data:
            return []
        includes = resp.includes or {}
        return [self._post_dict(post_from(t, includes, self.bot_id)) for t in resp.data]

    # ── Tools ────────────────────────────────────────────────────

    def _thread_fetch(self, post_id: str) -> dict:
        pid = status_id(str(post_id)) or re.sub(r"\D", "", str(post_id))
        if not pid:
            return {"error": "post_id must be a numeric id or an x.com post link"}
        post = fetch_post(self.cache, pid, self.bot_id)
        if not post:
            return {"error": "not found, deleted, or not public"}
        chain, _ = walk_up(self.cache, post, Config.THREAD_MAX_DEPTH, self.bot_id)
        result = {"thread": [self._post_dict(p) for p in chain]}
        if "x_keyword_search" in self._unavailable:
            result["replies_note"] = "replies are not available on the bot's X API plan"
            return result
        try:
            resp = search_recent(self.client, f"conversation_id:{post.conversation_id or pid}")
            result["replies"] = self._posts_from(resp)
        except (tweepy.errors.Forbidden, tweepy.errors.Unauthorized):
            self._unavailable.add("x_keyword_search")
            result["replies_note"] = "replies are not available on the bot's X API plan"
        except tweepy.errors.TooManyRequests:
            result["replies_note"] = "X rate limit reached; replies not fetched"
        return result

    def _keyword_search(self, query: str, sort: str = "Top") -> dict:
        q = (query or "").strip()
        if not q:
            return {"error": "query is empty"}
        if "is:retweet" not in q:
            q += " -is:retweet"
        resp = search_recent(self.client, q, latest=(sort == "Latest"))
        posts = self._posts_from(resp)
        return {"query": q, "sort": sort, "window": "last 7 days", "posts": posts}

    def _user_lookup(self, username: str) -> dict:
        handle = (username or "").strip().lstrip("@")
        if not _USERNAME_RE.match(handle):
            return {"error": "not a valid X handle"}
        resp = get_user_by_username(self.client, handle)
        user = getattr(resp, "data", None)
        if not user:
            return {"error": "no such account, or it is suspended"}
        metrics = getattr(user, "public_metrics", None) or {}
        bio = getattr(user, "description", "") or ""
        if not screen_input_for_injection(bio)[0]:
            bio = WITHHELD
        profile = {
            "username": user.username,
            "name": getattr(user, "name", None),
            "bio": bio,
            "verified": getattr(user, "verified", None),
            "followers": metrics.get("followers_count"),
            "following": metrics.get("following_count"),
            "posts": metrics.get("tweet_count"),
            "joined": str(getattr(user, "created_at", "") or "")[:10] or None,
        }
        result = {"profile": profile}
        try:
            result["recent_posts"] = self._posts_from(get_user_posts(self.client, user.id))
        except (tweepy.errors.Forbidden, tweepy.errors.Unauthorized):
            result["recent_posts_note"] = "recent posts are not available on the bot's X API plan"
        except tweepy.errors.TooManyRequests:
            result["recent_posts_note"] = "X rate limit reached; recent posts not fetched"
        return result

    def _view_image(self, url: str) -> dict:
        host = (urlparse(url or "").hostname or "").lower()
        if host not in Config.X_MEDIA_HOSTS:
            return {"error": "only X media URLs (pbs.twimg.com) can be viewed"}
        if self._images_added >= Config.MAX_IMAGES:
            return {"error": "image limit reached for this reply"}
        got = self.downloader(url)
        if not got:
            return {"error": "could not load that image"}
        self._images_added += 1
        self._images.append(XImage(got[0], got[1], f"image from {url}"))
        return {"ok": True, "note": "the image is attached in the next message"}
