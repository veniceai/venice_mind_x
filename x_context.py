"""X thread context for a mention, built the way @grok receives it (plan 0002).

Walks the reply chain from the mention up to the first post, with each post's
author, time and full text (long posts included), the posts they quote, x.com
links resolved through the API, and their images and video thumbnails. The
result renders as plain text for the model and carries the images separately.
"""
import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional, Tuple

import tweepy

from config import Config
from image_processor import download_image
from utils import extract_urls_from_entities

logger = logging.getLogger(__name__)

_STATUS_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/(?:[A-Za-z0-9_]+|i(?:/web)?)/status(?:es)?/(\d+)",
    re.IGNORECASE,
)
_MEDIA_LABELS = {"photo": "photo", "video": "video thumbnail", "animated_gif": "GIF thumbnail"}


def status_id(url: str) -> Optional[str]:
    """Post id from an x.com / twitter.com status link, or None."""
    m = _STATUS_RE.match(url or "")
    return m.group(1) if m else None


def full_text(tweet) -> str:
    """The post's whole text: note_tweet for long posts, else `text`."""
    raw = getattr(tweet, "data", None)
    note = raw.get("note_tweet") if isinstance(raw, dict) else None
    if isinstance(note, dict) and note.get("text"):
        return note["text"]
    return getattr(tweet, "text", "") or ""


_MEDIA_LINK_RE = re.compile(r"/status(?:es)?/\d+/(?:photo|video)/\d+")


def expand_links(text: str, tweet) -> str:
    """Swap t.co short links for their real URLs; drop links to the post's own media."""
    raw = getattr(tweet, "data", None)
    note = raw.get("note_tweet") if isinstance(raw, dict) else None
    entity_sets = [getattr(tweet, "entities", None) or {}]
    if isinstance(note, dict):
        entity_sets.append(note.get("entities") or {})
    for entities in entity_sets:
        for u in entities.get("urls", []) or []:
            short = u.get("url")
            real = u.get("unwound_url") or u.get("expanded_url")
            if not short or not real or short not in text:
                continue
            text = text.replace(short, "" if _MEDIA_LINK_RE.search(real) else real)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _fmt_time(value) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value.strftime("%Y-%m-%d %H:%M UTC")


@dataclass
class XPost:
    id: str
    text: str
    username: Optional[str] = None
    name: Optional[str] = None
    created_at: Optional[str] = None
    conversation_id: Optional[str] = None
    reply_to: Optional[str] = None
    quote_of: Optional[str] = None
    quoted: Optional["XPost"] = None
    media: List[dict] = field(default_factory=list)       # {"type", "url"}
    media_keys: List[str] = field(default_factory=list)
    urls: List[str] = field(default_factory=list)
    mine: bool = False

    @property
    def author(self) -> str:
        if self.mine:
            return f"@{self.username} (you)" if self.username else "you"
        if not self.username:
            return "unknown author"
        return f"@{self.username} ({self.name})" if self.name else f"@{self.username}"

    @property
    def permalink(self) -> str:
        return f"https://x.com/{self.username or 'i/web'}/status/{self.id}"

    def header(self) -> str:
        parts = [self.author]
        if self.created_at:
            parts.append(self.created_at)
        parts.append(f"post {self.id}")
        if self.reply_to:
            parts.append(f"reply to {self.reply_to}")
        return " · ".join(parts)

    def media_summary(self) -> str:
        counts: dict = {}
        for m in self.media:
            label = _MEDIA_LABELS.get(m["type"], m["type"])
            counts[label] = counts.get(label, 0) + 1
        return ", ".join(f"{n} {label}{'s' if n > 1 else ''}" for label, n in counts.items())


def post_from(tweet, includes: Optional[dict] = None, bot_id=None, *, with_quote: bool = True) -> XPost:
    """Build an XPost from a tweepy Tweet plus the response's includes."""
    includes = includes or {}
    users = {str(u.id): u for u in includes.get("users", []) or []}
    media = {m.media_key: m for m in includes.get("media", []) or []}
    tweets = {str(t.id): t for t in includes.get("tweets", []) or []}

    author_id = str(getattr(tweet, "author_id", "") or "")
    user = users.get(author_id)
    reply_to = quote_of = None
    for ref in getattr(tweet, "referenced_tweets", None) or []:
        if ref.type == "replied_to":
            reply_to = str(ref.id)
        elif ref.type == "quoted":
            quote_of = str(ref.id)

    keys = list((getattr(tweet, "attachments", None) or {}).get("media_keys", []))
    items = []
    for key in keys:
        m = media.get(key)
        # Photos carry `url`; videos and GIFs only a `preview_image_url` thumbnail.
        url = (getattr(m, "url", None) or getattr(m, "preview_image_url", None)) if m else None
        if url:
            items.append({"type": m.type, "url": url})

    post = XPost(
        id=str(tweet.id),
        text=expand_links(full_text(tweet), tweet),
        username=getattr(user, "username", None),
        name=getattr(user, "name", None),
        created_at=_fmt_time(getattr(tweet, "created_at", None)),
        conversation_id=str(getattr(tweet, "conversation_id", "") or "") or None,
        reply_to=reply_to,
        quote_of=quote_of,
        media=items,
        media_keys=keys,
        urls=extract_urls_from_entities(getattr(tweet, "entities", None)),
        mine=bool(bot_id) and author_id == str(bot_id),
    )
    if with_quote and quote_of and quote_of in tweets:
        post.quoted = post_from(tweets[quote_of], includes, bot_id, with_quote=False)
    return post


def fetch_post(cache, post_id, bot_id=None) -> Optional[XPost]:
    """Look up one post through the TweetCache. Raises tweepy rate-limit errors."""
    resp = cache.get(post_id)
    if not resp or not resp.data:
        return None
    return post_from(resp.data, resp.includes or {}, bot_id)


def walk_up(cache, start: XPost, max_depth: int, bot_id=None) -> Tuple[List[XPost], bool]:
    """Follow replied_to links from `start`. Returns (posts oldest first, rate_limited)."""
    chain = [start]
    cur = start
    for _ in range(max_depth):
        if not cur.reply_to:
            break
        try:
            parent = fetch_post(cache, cur.reply_to, bot_id)
        except tweepy.errors.TooManyRequests:
            logger.warning("Rate limited while walking a thread; using what was fetched")
            return chain, True
        except Exception as e:
            logger.warning(f"Could not fetch post {cur.reply_to}: {e}")
            break
        if not parent:
            break
        chain.insert(0, parent)
        cur = parent
    return chain, False


@dataclass
class XImage:
    data: bytes
    mime: str
    label: str

    def data_uri(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"


@dataclass
class ThreadContext:
    posts: List[XPost]                 # oldest first; the mention is last
    linked: List[XPost] = field(default_factory=list)
    images: List[XImage] = field(default_factory=list)
    gap_after_root: bool = False       # the walk stopped before reaching the root
    rate_limited: bool = False

    @property
    def mention(self) -> XPost:
        return self.posts[-1]

    def all_posts(self) -> List[XPost]:
        out = []
        for p in self.posts + self.linked:
            out.append(p)
            if p.quoted:
                out.append(p.quoted)
        return out

    @property
    def urls(self) -> List[str]:
        """Non-X links from every post, for URL screening and web scraping."""
        urls = [u for p in self.all_posts() for u in p.urls if not status_id(u)]
        return list(dict.fromkeys(urls))

    @property
    def permalinks(self) -> List[str]:
        return list(dict.fromkeys(p.permalink for p in self.all_posts()))

    def participants(self) -> List[str]:
        """Handles a reply may @mention: thread authors other than the asker and the bot."""
        asker = (self.mention.username or "").lower()
        return list(dict.fromkeys(
            p.username for p in self.posts
            if p.username and not p.mine and p.username.lower() != asker
        ))

    def render(self) -> str:
        lines = ["X THREAD (oldest first):"]
        for i, p in enumerate(self.posts, 1):
            tag = " · TAGGED YOU" if p is self.mention else ""
            lines.append(f"[{i}] {p.header()}{tag}")
            lines.append(p.text)
            if p.quoted:
                lines.append(f"  quoting {p.quoted.header()}:")
                lines.append("  " + p.quoted.text.replace("\n", "\n  "))
            if p.media:
                lines.append(f"  attached: {p.media_summary()}")
            if i == 1 and self.gap_after_root:
                lines.append("[… earlier replies not shown …]")
        if self.linked:
            lines.append("")
            lines.append("POSTS LINKED IN THE THREAD:")
            for p in self.linked:
                lines.append(f"- {p.header()}")
                lines.append(p.text)
        if self.images:
            lines.append("")
            labels = "; ".join(f"{i}) {img.label}" for i, img in enumerate(self.images, 1))
            lines.append(f"IMAGES ATTACHED (in order): {labels}")
        return "\n".join(lines)


def question_text(ctx: ThreadContext, bot_username: Optional[str]) -> str:
    """The mention's text without the bot's handle or the leading reply @handles."""
    text = ctx.mention.text
    thread_handles = {p.username.lower() for p in ctx.posts if p.username}
    if bot_username:
        thread_handles.add(bot_username.lower())
    # X prefixes replies with the @handles of the people being replied to.
    while True:
        m = re.match(r"\s*@(\w+)\s*", text)
        if not (m and m.group(1).lower() in thread_handles):
            break
        text = text[m.end():]
    if bot_username:
        text = re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text or "(The post only tags you. Explain the post it replies to.)"


def _collect_images(ctx: ThreadContext, downloader: Callable) -> List[XImage]:
    """Nearest posts first: the mention, its parent, their quotes, the root, links."""
    order: List[XPost] = []
    near = list(reversed(ctx.posts[-2:]))
    order.extend(near)
    order.extend(p.quoted for p in near if p.quoted)
    if len(ctx.posts) > 2:
        order.append(ctx.posts[0])
    order.extend(ctx.linked)
    order.extend(reversed(ctx.posts[1:-2]))

    images, seen = [], set()
    for p in order:
        for m in p.media:
            if len(images) >= Config.MAX_IMAGES:
                return images
            if m["url"] in seen:
                continue
            seen.add(m["url"])
            got = downloader(m["url"])
            if got:
                label = f"{_MEDIA_LABELS.get(m['type'], m['type'])} from post {p.id} ({p.author})"
                images.append(XImage(got[0], got[1], label))
    return images


def build_context(
    cache,
    mention,
    includes: Optional[dict] = None,
    *,
    bot_id=None,
    downloader: Callable = download_image,
) -> ThreadContext:
    """Assemble the thread around `mention` (a tweepy Tweet from the mentions poll)."""
    start = post_from(mention, includes, bot_id)
    chain, rate_limited = walk_up(cache, start, Config.THREAD_MAX_DEPTH, bot_id)
    ctx = ThreadContext(posts=chain, rate_limited=rate_limited)

    known = {p.id for p in chain}
    root_id = start.conversation_id
    if chain[0].reply_to and root_id and root_id not in known and not rate_limited:
        try:
            root = fetch_post(cache, root_id, bot_id)
            if root:
                chain.insert(0, root)
                known.add(root.id)
                ctx.gap_after_root = True
        except Exception as e:
            logger.warning(f"Could not fetch thread root {root_id}: {e}")

    # Quotes of the mention and its parent are fetched in full for their media.
    for p in chain[-2:]:
        if not p.quote_of or rate_limited:
            continue
        if p.quoted and (p.quoted.media or not p.quoted.media_keys):
            continue
        try:
            q = fetch_post(cache, p.quote_of, bot_id)
            if q:
                q.quoted = None
                p.quoted = q
        except Exception as e:
            logger.warning(f"Could not fetch quoted post {p.quote_of}: {e}")
    known.update(p.quoted.id for p in chain if p.quoted)

    # x.com links in the mention or its parent go through the API, not the scraper.
    for p in reversed(chain[-2:]):
        for url in p.urls:
            sid = status_id(url)
            if not sid or sid in known or len(ctx.linked) >= Config.MAX_LINKED_POSTS or rate_limited:
                continue
            known.add(sid)
            try:
                linked = fetch_post(cache, sid, bot_id)
                if linked:
                    ctx.linked.append(linked)
            except Exception as e:
                logger.warning(f"Could not fetch linked post {sid}: {e}")

    ctx.images = _collect_images(ctx, downloader)
    return ctx
