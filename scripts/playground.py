#!/usr/bin/env python3
"""Local test page for @venice_mind: build a thread, tag the bot, read the reply.

Runs the real pipeline (Clef routing, the lane models, web search, the crafter,
X rules and the gate) on a thread you write in the browser. Nothing touches X
unless X keys are set in .env; then the bot's X tools and "load a real post"
read from the X API. Nothing is ever posted.

    .venv/bin/python scripts/playground.py          # http://localhost:3000
    .venv/bin/python scripts/playground.py 3001
"""
import base64
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
os.chdir(ROOT)

from config import Config  # noqa: E402
from finalize import finalize_reply  # noqa: E402
from router import ClefUnavailable  # noqa: E402
from venice_api import analyse, craft_tweet, get_trace  # noqa: E402
from x_context import build_context, question_text  # noqa: E402
from x_fakes import FakeCache, media, resp, tweet, user  # noqa: E402
from x_tools import XToolbox  # noqa: E402

logger = logging.getLogger("playground")
PAGE = os.path.join(ROOT, "scripts", "playground.html")
BOT_HANDLE = "venice_mind"
BOT_ID = 999
_lock = threading.Lock()   # the pipeline keeps a per-mention trace in module state


def _x_keys_set() -> bool:
    keys = [Config.TWITTER_BEARER_TOKEN, Config.TWITTER_API_KEY, Config.TWITTER_API_SECRET,
            Config.TWITTER_ACCESS_TOKEN, Config.TWITTER_ACCESS_TOKEN_SECRET]
    return all(k and not k.startswith("your_") for k in keys)


_x = {"client": None, "me": None, "error": None}


def _x_client():
    """The real tweepy client when X keys are in .env (read-only use here), else None."""
    if _x["client"] or _x["error"] or not _x_keys_set():
        return _x["client"]
    try:
        from clients import get_twitter_client
        client = get_twitter_client()
        me = client.get_me().data
        _x.update(client=client, me=me)
        logger.info(f"X API connected as @{me.username} (read-only in the playground)")
    except Exception as e:
        _x["error"] = str(e)
        logger.warning(f"X keys are set but the X API didn't answer: {e}")
    return _x["client"]


class _NoX:
    """Stands in for the X API when no keys are set: the tools say so to the model."""

    def _deny(self, *a, **k):
        import tweepy
        raise tweepy.errors.Forbidden(SimpleNamespace(status_code=403, reason="", json=lambda: {}, text=""))

    search_recent_tweets = get_user = get_users_tweets = _deny


def _decode_image(data_uri):
    if not data_uri:
        return None
    m = re.match(r"data:(image/[\w.+-]+);base64,(.+)", data_uri, re.S)
    return (base64.b64decode(m.group(2)), m.group(1)) if m else None


def _build_local(body):
    """A fake thread from the page: posts oldest first, then the mention."""
    posts, users, images = {}, {}, {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    prev_id = None
    for i, p in enumerate(body.get("posts") or [], start=1):
        handle = (p.get("handle") or f"user{i}").lstrip("@")
        uid = users.setdefault(handle.lower(), user(100 + i, handle, p.get("name") or None))
        keys, media_items = [], []
        img = _decode_image(p.get("image"))
        if img:
            url = f"https://pbs.twimg.com/media/local-{i}.png"
            images[url] = img
            keys, media_items = [f"m{i}"], [media(f"m{i}", url=url)]
        posts[i] = resp(tweet(i, p.get("text") or "", uid.id, reply_to=prev_id, conversation_id=1,
                              media_keys=keys, created_at=now), users=[uid], media_=media_items)
        prev_id = i
    m = body.get("mention") or {}
    handle = (m.get("handle") or "you").lstrip("@")
    asker = users.setdefault(handle.lower(), user(50, handle, None))
    keys, media_items = [], []
    img = _decode_image(m.get("image"))
    if img:
        url = "https://pbs.twimg.com/media/local-mention.png"
        images[url] = img
        keys, media_items = ["mm"], [media("mm", url=url)]
    mention = tweet(1000, m.get("text") or "", asker.id, reply_to=prev_id, conversation_id=1 if prev_id else 1000,
                    media_keys=keys, created_at=now)
    includes = {"users": [asker], "media": media_items}
    return FakeCache(posts), mention, includes, (lambda url: images.get(url)), _NoX()


def _build_from_x(url):
    """A real post by link, through the X API (read-only)."""
    from twitter_client import TweetCache, get_tweet_by_id
    from image_processor import download_image
    from x_context import status_id
    client = _x_client()
    if not client:
        raise ValueError("Loading a real post needs the five X keys in .env.")
    sid = status_id(url) or (url if url.isdigit() else None)
    if not sid:
        raise ValueError("That isn't an x.com post link.")
    r = get_tweet_by_id(client, sid)
    if not r or not r.data:
        raise ValueError("X didn't return that post (deleted, private, or not found).")
    return TweetCache(client), r.data, r.includes or {}, download_image, client


def _citations(evidence):
    out = []
    for e in evidence:
        kind = e.split("]", 1)[0].lstrip("[")
        url = (re.findall(r"https?://\S+", e) or [""])[-1]
        text = e.split("] ", 1)[1] if "] " in e else e
        title = text.split(": ", 1)[0]
        out.append({"kind": kind, "title": title[:140], "url": url})
    return out


def run_reply(body):
    Config.X_PREMIUM_ENABLED = bool(body.get("premium"))
    if body.get("x_url"):
        cache, mention, includes, downloader, client = _build_from_x(body["x_url"].strip())
        bot_id = getattr(_x["me"], "id", BOT_ID)
        bot_handle = getattr(_x["me"], "username", BOT_HANDLE)
    else:
        cache, mention, includes, downloader, client = _build_local(body)
        real = _x_client()
        client = real or client
        bot_id, bot_handle = getattr(_x["me"], "id", BOT_ID), getattr(_x["me"], "username", BOT_HANDLE)

    ctx = build_context(cache, mention, includes, bot_id=bot_id, downloader=downloader)
    box = XToolbox(client, cache, bot_id=bot_id, downloader=downloader)
    tools, run_tool = [], box.run
    box.run = lambda name, args: tools.append({"tool": name, "args": args}) or run_tool(name, args)
    evidence, query = [], question_text(ctx, bot_handle)
    t = time.time()
    out = {"query": query, "thread": ctx.render(), "images": len(ctx.images), "drafted": None,
           "reply": None, "failed": None, "x_api": bool(_x["client"]),
           "x_error": _x["error"], "premium": Config.X_PREMIUM_ENABLED, "target": Config.reply_target()}
    try:
        analysis = analyse(query, context=ctx.render(), images=ctx.images, urls=ctx.urls,
                           toolbox=box, evidence=evidence)
        if analysis == Config.ERROR_MESSAGE:
            out["failed"] = "no model gave a usable answer (errors or refusals)"
        else:
            out["drafted"] = craft_tweet(analysis, use_vision=bool(ctx.images),
                                         context_urls=ctx.urls + ctx.permalinks + box.permalinks)
            out["reply"], out["failed"] = finalize_reply(
                out["drafted"], thread=ctx.render(), sources="\n".join(evidence + box.evidence),
                participants=ctx.participants())
    except ClefUnavailable as e:
        out["failed"] = f"Clef unavailable ({e}); the live bot would hold this mention and retry"
    trace = get_trace()
    out.update(
        seconds=round(time.time() - t, 1), route=trace["route"], calls=trace["calls"],
        cost_usd=round(sum(c.get("cost_usd") or 0 for c in trace["calls"]), 4),
        tools=tools, sources=_citations(evidence + box.evidence),
    )
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info(fmt % args)

    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/status":
            _x_client()
            return self._send(200, {"x_keys": _x_keys_set(), "x_api": bool(_x["client"]),
                                    "x_user": getattr(_x["me"], "username", None), "x_error": _x["error"],
                                    "venice_key": bool(Config.VENICE_API_KEY)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/reply":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            with _lock:
                result = run_reply(body)
            self._send(200, result)
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            logger.exception("reply failed")
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    logger.info(f"Playground on http://localhost:{port} (local only; nothing is posted to X)")
    server.serve_forever()


if __name__ == "__main__":
    main()
