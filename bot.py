"""
Core bot logic — polls mentions, processes them through the Venice AI pipeline.
"""
import logging
import time
from datetime import datetime, timezone, timedelta

import tweepy

from config import Config
from state import State
from clients import get_twitter_client
from twitter_client import get_mentions, reply_to_tweet, TweetCache
from venice_api import analyse, craft_tweet
from venice_knowledge import validate_configured_models
from x_context import build_context, question_text
from x_tools import XToolbox

logger = logging.getLogger(__name__)


class VeniceBot:
    def __init__(self):
        logger.info("Initializing Venice X Bot …")
        self.state = State()
        self.state.load()
        self.client = get_twitter_client()
        self.bot_id, self.bot_username = self._fetch_bot_identity()
        self.session_start = datetime.now(timezone.utc)
        if Config.DRY_RUN:
            logger.warning("DRY_RUN is on — replies are logged, not posted.")

        # Cache immutable thread-context lookups (parent/root/quoted tweets) so
        # they aren't re-fetched from the API on every reply in a thread.
        self.tweet_cache = TweetCache(self.client)

        # Surface any retired/renamed model IDs at boot (Plan 0001) — better a
        # loud startup alert than silent per-reply degradation later.
        validate_configured_models()

        # Rate-limit bookkeeping
        self.last_check = 0.0
        self.hourly_replies = 0
        self.hourly_reset = time.time()
        self._backoff = 1
        
        # Per-user reply tracking (anti-spam)
        self.user_reply_counts: dict[str, int] = {}  # user_id → reply count this hour
        self.user_reply_reset = time.time()
        
        logger.info("Bot ready.")

    # ── Helpers ──────────────────────────────────────────────────

    def _fetch_bot_identity(self) -> tuple:
        """Get our own user ID and handle (with one rate-limit retry)."""
        for attempt in range(2):
            try:
                me = self.client.get_me()
                if me and me.data:
                    logger.info(f"Bot user: @{me.data.username} ({me.data.id})")
                    return me.data.id, me.data.username
                raise RuntimeError("get_me() returned no data")
            except tweepy.errors.TooManyRequests as e:
                if attempt == 0:
                    self._wait_for_rate_limit(e)
                else:
                    raise
        raise RuntimeError("Could not fetch bot user")

    def _wait_for_rate_limit(self, exc: tweepy.errors.TooManyRequests):
        reset = int(exc.response.headers.get("x-rate-limit-reset", 0))
        now = time.time()
        wait = max(reset - now + 5, 60)
        logger.warning(f"Rate limited. Sleeping {wait:.0f}s …")
        time.sleep(wait)

    def _backoff_sleep(self, base: float = None):
        base = base or Config.MIN_CHECK_INTERVAL
        wait = min(base * self._backoff, 900)
        logger.warning(f"Backing off {wait:.0f}s (×{self._backoff})")
        time.sleep(wait)
        self._backoff = min(self._backoff * 2, 16)

    def _reset_backoff(self):
        self._backoff = 1

    def _tick_hourly(self):
        if time.time() - self.hourly_reset >= 3600:
            logger.info(f"Hourly reset ({self.hourly_replies} replies this hour)")
            self.hourly_replies = 0
            self.hourly_reset = time.time()
        
        # Also reset per-user counts
        if time.time() - self.user_reply_reset >= 3600:
            self.user_reply_counts = {}
            self.user_reply_reset = time.time()
    
    def _is_blocked_account(self, author) -> bool:
        """Check if author is a blocked bot/spam account."""
        if not author:
            return False
        username = getattr(author, "username", None)
        if username and username.lower() in {b.lower() for b in Config.BLOCKED_ACCOUNTS}:
            logger.info(f"Blocked account ignored: @{username}")
            return True
        return False
    
    def _user_reply_limit_reached(self, user_id: str) -> bool:
        """Check if we've hit the per-user reply limit."""
        count = self.user_reply_counts.get(str(user_id), 0)
        if count >= Config.MAX_REPLIES_PER_USER_PER_HOUR:
            logger.info(f"Per-user limit reached for {user_id} ({count} replies this hour)")
            return True
        return False
    
    def _increment_user_replies(self, user_id: str):
        """Track reply to this user."""
        uid = str(user_id)
        self.user_reply_counts[uid] = self.user_reply_counts.get(uid, 0) + 1

    def _tweet_age_ok(self, tweet) -> bool:
        """Return True if tweet is recent enough to reply to."""
        created = getattr(tweet, "created_at", None)
        if created is None:
            return False
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                return False
        age = datetime.now(timezone.utc) - created
        if age > timedelta(minutes=Config.MAX_TWEET_AGE_MINUTES):
            logger.debug(f"Tweet {tweet.id} too old ({age})")
            return False
        return True

    # ── Processing ───────────────────────────────────────────────

    def _process_tweet(self, tweet, includes: dict, user_lookup: dict):
        if self.state.is_processed(tweet.id):
            return

        author = user_lookup.get(tweet.author_id)
        if not author or author.protected or author.id == self.bot_id:
            self.state.add_tweet(tweet.id)
            return

        # ── Anti-spam checks ──
        # 1. Blocked accounts (known bots)
        if self._is_blocked_account(author):
            self.state.add_tweet(tweet.id)
            return
        
        # 2. Per-user rate limit
        if self._user_reply_limit_reached(tweet.author_id):
            self.state.add_tweet(tweet.id)
            return
        
        # 3. Verified-only mode (optional)
        if Config.VERIFIED_ONLY and not getattr(author, "verified", False):
            self.state.add_tweet(tweet.id)
            return

        # Enforce single-author conversations
        allowed = self.state.get_allowed_author(tweet.conversation_id)
        if allowed and str(tweet.author_id) != str(allowed):
            self.state.add_tweet(tweet.id)
            return

        if not self._tweet_age_ok(tweet):
            self.state.add_tweet(tweet.id)
            return

        # ── Gather the thread, the way @grok sees it ──
        ctx = build_context(self.tweet_cache, tweet, includes, bot_id=self.bot_id)
        toolbox = XToolbox(self.client, self.tweet_cache, bot_id=self.bot_id)
        logger.info(
            f"Thread for {tweet.id}: {len(ctx.posts)} post(s), {len(ctx.linked)} linked, "
            f"{len(ctx.images)} image(s){' (rate limited)' if ctx.rate_limited else ''}"
        )

        # ── Generate reply ──
        analysis = analyse(
            question_text(ctx, self.bot_username),
            context=ctx.render(),
            images=ctx.images,
            urls=ctx.urls,
            toolbox=toolbox,
        )
        if analysis == Config.ERROR_MESSAGE:
            logger.warning(f"Analysis failed for tweet {tweet.id}")
            return

        allowed_links = ctx.urls + ctx.permalinks + toolbox.permalinks
        final = craft_tweet(analysis, use_vision=bool(ctx.images), context_urls=allowed_links)
        if not final or final == Config.ERROR_MESSAGE:
            logger.warning(f"Crafting failed for tweet {tweet.id}")
            return

        if Config.DRY_RUN:
            logger.info(f"[DRY_RUN] Would reply to {tweet.id} ({len(final)} chars, "
                        f"{toolbox.posts_fetched} post(s) fetched by tools):\n{final}")
            self.state.add_tweet(tweet.id)
            return

        # ── Post reply ──
        try:
            resp = reply_to_tweet(self.client, tweet.id, final)
            if resp:
                self.hourly_replies += 1
                self._increment_user_replies(tweet.author_id)
                if not self.state.get_allowed_author(tweet.conversation_id):
                    self.state.set_allowed_author(tweet.conversation_id, tweet.author_id)
                self.state.add_tweet(tweet.id)
                logger.info(f"Replied to tweet {tweet.id} (user {tweet.author_id}: {self.user_reply_counts.get(str(tweet.author_id), 0)} this hour)")
            else:
                logger.warning(f"Reply returned None for tweet {tweet.id}")
        except tweepy.errors.TooManyRequests as e:
            self._wait_for_rate_limit(e)
        except Exception as e:
            logger.error(f"Error posting reply: {e}")

        time.sleep(Config.TWEET_DELAY)

    # ── Main loop ────────────────────────────────────────────────

    def process_mentions(self):
        # Rate-limit pacing
        elapsed = time.time() - self.last_check
        if elapsed < Config.MIN_CHECK_INTERVAL:
            time.sleep(Config.MIN_CHECK_INTERVAL - elapsed)

        self._tick_hourly()
        if self.hourly_replies >= Config.MAX_REPLIES_PER_HOUR:
            logger.warning("Hourly reply cap reached.")
            return

        # Lookback window
        if Config.USE_SESSION_START_CUTOFF:
            cutoff = max(
                self.session_start,
                datetime.now(timezone.utc) - timedelta(minutes=Config.MAX_TWEET_AGE_MINUTES),
            )
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=Config.MAX_TWEET_AGE_MINUTES)

        start_time = cutoff.strftime("%Y-%m-%dT%H:%M:%S.") + f"{cutoff.microsecond // 1000:03d}Z"

        try:
            mentions = get_mentions(self.client, self.bot_id, start_time=start_time)
            self.last_check = time.time()

            if not mentions or not mentions.data:
                logger.info("No new mentions.")
                self._reset_backoff()
                return

            includes = mentions.includes or {}
            user_lookup = {u.id: u for u in includes.get("users", [])}

            logger.info(f"Processing {len(mentions.data)} mention(s) …")
            for tweet in reversed(mentions.data):
                # Session cutoff guard
                if Config.USE_SESSION_START_CUTOFF:
                    created = getattr(tweet, "created_at", None)
                    if created:
                        try:
                            dt = (
                                created
                                if not isinstance(created, str)
                                else datetime.fromisoformat(created.replace("Z", "+00:00"))
                            )
                            if dt < self.session_start:
                                self.state.add_tweet(tweet.id)
                                continue
                        except Exception:
                            pass

                try:
                    self._process_tweet(tweet, includes, user_lookup)
                except Exception as e:
                    logger.error(f"Unhandled error on tweet {tweet.id}: {e}", exc_info=True)
                    self.state.add_tweet(tweet.id)

            self._reset_backoff()

        except tweepy.errors.TooManyRequests as e:
            self._wait_for_rate_limit(e)
        except tweepy.errors.TweepyException as e:
            logger.error(f"Twitter API error: {e}")
            self._backoff_sleep()
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            self._backoff_sleep()

    def run(self):
        logger.info("Venice X Bot is live. 🚀")
        while True:
            try:
                self.process_mentions()
                self.state.save()
            except KeyboardInterrupt:
                logger.info("Shutting down …")
                self.state.save()
                break
            except Exception as e:
                logger.critical(f"Critical error in main loop: {e}", exc_info=True)
                time.sleep(60)
