"""Plan 0002 — the X thread context a mention gets (offline; X is faked)."""
from config import Config
from x_context import build_context, full_text, post_from, question_text, status_id
from x_fakes import FakeCache, fake_downloader, media, resp, too_many_requests, tweet, user

BOT_ID = 999
ALICE, BOB, CAROL = user(1, "alice", "Alice"), user(2, "bob"), user(3, "carol")
BOT = user(BOT_ID, "venice_mind", "Venice")


def _mention(text="@bob @venice_mind is this true?", **kw):
    kw.setdefault("reply_to", 101)
    kw.setdefault("conversation_id", 100)
    return tweet(102, text, 3, **kw)


def _thread_cache(**extra):
    posts = {
        100: resp(tweet(100, "The bridge cost 3x its budget.", 1, media_keys=["m1"]),
                  users=[ALICE], media_=[media("m1", url="https://pbs.twimg.com/media/a.jpg")]),
        101: resp(tweet(101, "Source?", 2, reply_to=100, conversation_id=100), users=[BOB]),
    }
    posts.update(extra)
    return FakeCache(posts)


def test_full_text_prefers_note_tweet():
    t = tweet(1, "truncated preview…", 1, note="the whole long post " * 40)
    assert full_text(t).startswith("the whole long post")
    assert full_text(tweet(2, "short", 1)) == "short"


def test_status_id_parses_x_and_twitter_links():
    assert status_id("https://x.com/alice/status/123") == "123"
    assert status_id("https://twitter.com/i/web/status/456?s=20") == "456"
    assert status_id("https://mobile.twitter.com/bob/statuses/789") == "789"
    assert status_id("https://example.com/status/1") is None


def test_walks_the_whole_chain_oldest_first_with_authors():
    ctx = build_context(_thread_cache(), _mention(), {"users": [CAROL]}, bot_id=BOT_ID,
                        downloader=fake_downloader)
    assert [p.id for p in ctx.posts] == ["100", "101", "102"]
    assert [p.username for p in ctx.posts] == ["alice", "bob", "carol"]
    text = ctx.render()
    assert text.index("The bridge cost") < text.index("Source?") < text.index("is this true?")
    assert "@alice (Alice)" in text and "TAGGED YOU" in text
    assert "2026-11-09 14:00 UTC" in text
    assert not ctx.gap_after_root


def test_long_posts_render_in_full():
    long = "word " * 200
    cache = FakeCache({101: resp(tweet(101, "word word…", 2, note=long), users=[BOB])})
    ctx = build_context(cache, _mention(conversation_id=101), {"users": [CAROL]}, bot_id=BOT_ID)
    assert long.strip() in ctx.render()


def test_bot_posts_are_marked_as_yours():
    cache = FakeCache({101: resp(tweet(101, "Earlier answer", BOT_ID, conversation_id=101), users=[BOT])})
    ctx = build_context(cache, _mention(conversation_id=101), {"users": [CAROL]}, bot_id=BOT_ID)
    assert ctx.posts[0].mine
    assert "@venice_mind (you)" in ctx.render()


def test_depth_cap_adds_the_root_with_a_gap(monkeypatch):
    monkeypatch.setattr(Config, "THREAD_MAX_DEPTH", 2)
    posts = {
        1: resp(tweet(1, "root", 1), users=[ALICE]),
        2: resp(tweet(2, "two", 2, reply_to=1, conversation_id=1), users=[BOB]),
        3: resp(tweet(3, "three", 1, reply_to=2, conversation_id=1), users=[ALICE]),
        4: resp(tweet(4, "four", 2, reply_to=3, conversation_id=1), users=[BOB]),
    }
    cache = FakeCache(posts)
    mention = tweet(5, "@venice_mind ?", 3, reply_to=4, conversation_id=1)
    ctx = build_context(cache, mention, {"users": [CAROL]}, bot_id=BOT_ID)
    assert [p.id for p in ctx.posts] == ["1", "3", "4", "5"]
    assert ctx.gap_after_root
    assert "earlier replies not shown" in ctx.render()
    assert "2" not in cache.calls


def test_rate_limit_stops_the_walk_without_raising():
    cache = FakeCache({101: too_many_requests()})
    ctx = build_context(cache, _mention(), {"users": [CAROL]}, bot_id=BOT_ID)
    assert ctx.rate_limited
    assert [p.id for p in ctx.posts] == ["102"]


def test_quote_of_the_mention_is_fetched_for_its_media():
    quoted_inline = tweet(50, "quoted text", 1, media_keys=["q1"])
    mention = tweet(102, "@venice_mind what is this?", 3, quote_of=50)
    cache = FakeCache({50: resp(tweet(50, "quoted text", 1, media_keys=["q1"]), users=[ALICE],
                                media_=[media("q1", url="https://pbs.twimg.com/media/q.jpg")])})
    ctx = build_context(cache, mention, {"users": [CAROL, ALICE], "tweets": [quoted_inline]},
                        bot_id=BOT_ID, downloader=fake_downloader)
    assert ctx.mention.quoted.media[0]["url"].endswith("q.jpg")
    assert "quoting @alice (Alice)" in ctx.render()
    assert len(ctx.images) == 1 and "post 50" in ctx.images[0].label


def test_x_links_resolve_through_the_api_not_the_scraper():
    mention = tweet(102, "@venice_mind is this real?", 3,
                    urls=["https://x.com/alice/status/700", "https://example.com/article"])
    cache = FakeCache({700: resp(tweet(700, "Linked claim", 1), users=[ALICE])})
    ctx = build_context(cache, mention, {"users": [CAROL]}, bot_id=BOT_ID)
    assert [p.id for p in ctx.linked] == ["700"]
    assert "Linked claim" in ctx.render()
    assert ctx.urls == ["https://example.com/article"]
    assert "https://x.com/alice/status/700" in ctx.permalinks


def test_video_thumbnails_count_as_images_and_cap_applies(monkeypatch):
    monkeypatch.setattr(Config, "MAX_IMAGES", 2)
    keys = ["v1", "p1", "p2"]
    mention = tweet(102, "@venice_mind ?", 3, media_keys=keys)
    includes = {"users": [CAROL], "media": [
        media("v1", "video", preview="https://pbs.twimg.com/thumb/v.jpg"),
        media("p1", url="https://pbs.twimg.com/media/1.jpg"),
        media("p2", url="https://pbs.twimg.com/media/2.jpg"),
    ]}
    ctx = build_context(FakeCache(), mention, includes, bot_id=BOT_ID, downloader=fake_downloader)
    assert len(ctx.images) == 2
    assert ctx.images[0].label.startswith("video thumbnail")
    assert "1 video thumbnail, 2 photos" in ctx.render()


def test_images_nearest_post_first():
    cache = _thread_cache()
    mention = _mention(media_keys=["mm"])
    includes = {"users": [CAROL], "media": [media("mm", url="https://pbs.twimg.com/media/m.jpg")]}
    ctx = build_context(cache, mention, includes, bot_id=BOT_ID, downloader=fake_downloader)
    assert [img.label.split(" from ")[1].split(" ")[1] for img in ctx.images] == ["102", "100"]


def test_question_text_strips_reply_handles_and_the_bot():
    ctx = build_context(_thread_cache(), _mention(), {"users": [CAROL]}, bot_id=BOT_ID)
    assert question_text(ctx, "venice_mind") == "is this true?"


def test_question_text_keeps_handles_the_user_typed():
    mention = tweet(102, "@venice_mind did @elonmusk really say this?", 3)
    ctx = build_context(FakeCache(), mention, {"users": [CAROL]}, bot_id=BOT_ID)
    assert question_text(ctx, "venice_mind") == "did @elonmusk really say this?"


def test_bare_tag_asks_to_explain_the_post():
    ctx = build_context(_thread_cache(), _mention(text="@bob @venice_mind"), {"users": [CAROL]}, bot_id=BOT_ID)
    assert "Explain the post" in question_text(ctx, "venice_mind")


def test_post_from_handles_missing_includes():
    p = post_from(tweet(9, "hi", 77), None)
    assert p.username is None and p.author == "unknown author"
    assert p.permalink == "https://x.com/i/web/status/9"


def test_tco_links_expand_and_media_links_drop():
    mention = tweet(102, "@venice_mind is this real? https://t.co/aaa https://t.co/ppp", 3, urls=[
        {"url": "https://t.co/aaa", "expanded_url": "https://example.com/report"},
        {"url": "https://t.co/ppp", "expanded_url": "https://x.com/carol/status/102/photo/1"},
    ])
    ctx = build_context(FakeCache(), mention, {"users": [CAROL]}, bot_id=BOT_ID)
    assert ctx.mention.text == "@venice_mind is this real? https://example.com/report"
    assert "t.co" not in ctx.render()
