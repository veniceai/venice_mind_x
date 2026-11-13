#!/usr/bin/env python3
"""
Manual reply tool — run the live pipeline on one post and ask before posting.
Usage: python reply_to_tweet.py <tweet_url_or_id> [--dry]
  --dry   print the reply and exit without asking to post
"""
import sys
import os
import re
from typing import Optional

# Ensure imports work from any directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from clients import get_twitter_client
from finalize import finalize_reply
from router import ClefUnavailable, route
from twitter_client import get_tweet_by_id, reply_to_tweet, TweetCache
from venice_api import analyse, craft_tweet
from safety import classify_url
from x_context import build_context, question_text
from x_tools import XToolbox


def extract_tweet_id(url: str) -> Optional[str]:
    m = re.search(r"/status/(\d+)", url)
    if m:
        return m.group(1)
    return url if url.isdigit() else None


def main():
    args = [a for a in sys.argv[1:] if a != "--dry"]
    dry = "--dry" in sys.argv[1:]
    if len(args) != 1:
        print("Usage: python reply_to_tweet.py <tweet_url_or_id> [--dry]")
        sys.exit(1)

    tweet_id = extract_tweet_id(args[0])
    if not tweet_id:
        print("❌ Bad input — need a tweet URL or numeric ID")
        sys.exit(1)

    print(f"🐦 Tweet ID: {tweet_id}")

    client = get_twitter_client()
    me = client.get_me().data
    print(f"✅ Twitter connected as @{me.username}")

    resp = get_tweet_by_id(client, tweet_id)
    if not resp or not resp.data:
        print("❌ Tweet not found")
        sys.exit(1)

    cache = TweetCache(client)
    ctx = build_context(cache, resp.data, resp.includes or {}, bot_id=me.id)
    query = question_text(ctx, me.username)
    print(f"\n{ctx.render()}\n")
    print(f"❓ Question: {query}")
    for u in ctx.urls:
        print(f"   {classify_url(u):>10} │ {u}")

    try:
        r = route(query, ctx.render(), bool(ctx.images))
        print(f"🧭 Lane: {r.lane} (charged={r.charged}, filtered={r.filtered}, complex={r.complex}) {r.reason}")

        toolbox = XToolbox(client, cache, bot_id=me.id)
        evidence: list = []
        print("🤖 Analysing …")
        analysis = analyse(query, context=ctx.render(), images=ctx.images, urls=ctx.urls,
                           toolbox=toolbox, evidence=evidence)
        print("✍️ Crafting tweet …")
        drafted = craft_tweet(
            analysis, use_vision=bool(ctx.images),
            context_urls=ctx.urls + ctx.permalinks + toolbox.permalinks,
        )
        final, line = finalize_reply(
            drafted, thread=ctx.render(), sources="\n".join(evidence + toolbox.evidence),
            participants=ctx.participants(),
        )
    except ClefUnavailable as e:
        print(f"⚠️ Clef is unavailable ({e}); the bot would hold this mention and retry on its next poll.")
        sys.exit(1)
    if line:
        print(f"⛔ The reply fails the {line} check; the bot would not post it. Draft:\n{drafted}")
        final = drafted

    print(f"\n{'=' * 60}")
    print(f"🎯 RESPONSE ({len(final)} chars, {toolbox.posts_fetched} post(s) fetched by tools):")
    print("=" * 60)
    print(final)
    print("=" * 60)
    if dry or line:
        return

    while True:
        choice = input("\nPost this reply? (y/n/edit): ").lower().strip()
        if choice in ("y", "yes"):
            try:
                r = reply_to_tweet(client, tweet_id, final)
                if r:
                    print(f"✅ Posted! https://x.com/{me.username}/status/{r.data['id']}")
                else:
                    print("❌ Post failed")
            except Exception as e:
                print(f"❌ {e}")
            break
        elif choice in ("n", "no"):
            print("👍 Not posted.")
            break
        elif choice == "edit":
            final = input("New response: ").strip() or final
            print(f"📏 {len(final)} chars")


if __name__ == "__main__":
    main()
