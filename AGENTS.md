# AGENTS.md

Guidance for AI coding agents (and humans) working in **`twitter_bot_venice`** —
the codebase behind **@venice_mind**, a Twitter/X bot powered by Venice AI.

> If you only read one thing: this bot talks to the public and handles
> anti-scam / anti-injection defences. Changes to `safety.py`, the system
> prompts in `config.py`, or the model cascade in `venice_api.py` are
> **high-blast-radius**. Treat them with care and test offline first.

---

## What this project is

A long-running Python process that polls its own Twitter mentions, runs each
mention through a Venice AI pipeline (Clef routing → analysis with X tools →
tweet crafting → `finalize.py`: X rules, token backstop, Clef gate), and posts a reply. It reads the whole reply chain with authors,
long posts, quotes, linked posts and media, calls X tools when it needs more,
answers questions about Venice's products from a committed FAQ snapshot + live
models API, and blocks crypto scams and prompt-injection attempts. It reads X
like the `@grok` bot and speaks in Venice's voice: uncensored, unbiased; see
`docs/plans/0002-grok-style-replies.md`. Operator setup: `docs/OPERATOR.md`.

There is **no web server, no database, and no build step.** State is a single
JSON file. It is deployed as a plain `python main.py` process.

---

## Layout

| File | Responsibility |
|---|---|
| `main.py` | Entry point. Configures logging, validates config, runs `VeniceBot`. |
| `bot.py` | Core loop: poll mentions → filter → gather thread context → generate → reply. Rate limiting, anti-spam, backoff, `DRY_RUN`, the `PAUSED` file check on every loop, and one reply-log record per handled mention. |
| `reply_log.py` | `log_reply()`: appends a JSON line to `logs/replies.jsonl` (route, models, cost, gate result, reply, posted). Never raises. The trace it logs comes from `venice_api.get_trace()`. |
| `config.py` | **Single source of truth** for settings, model IDs, and the two system prompts (`ANALYST_PROMPT`, `CRAFTER_PROMPT`). |
| `router.py` | One Clef call (`POST /decisions`, four yes/no questions) per mention → uncensored lane (DeepSeek) or complex lane (Kimi K3), or a decline for doxxing requests. `choose_lane` is pure. Raises `ClefUnavailable` when Clef can't answer; the bot then holds the mention for the next poll (up to `CLEF_MAX_ATTEMPTS`). |
| `reply_gate.py` | Clef check of the final reply, with the thread and evidence (web citations, fetched posts): the four hard lines, unsupported claims about real people or recent events, speculation or insinuation about anyone's health, private life or alleged wrongdoing (public figures included), VVV/DIEM returns. Raises `ClefUnavailable` (logged at ERROR) when Clef can't answer, so nothing posts unchecked. |
| `finalize.py` | `finalize_reply()`: `fit_x_rules`, the `mentions_token_returns` regex backstop, the gate; one rewrite for an unsupported claim or a token-return figure, otherwise not posted. |
| `venice_api.py` | Venice chat-completion calls. The two-pass `analyse()` + `craft_tweet()` pipeline, the function-calling tool loop, and the per-lane model cascade. |
| `x_context.py` | Builds the thread a mention gets: reply chain with authors, `note_tweet` full text, quotes, x.com links via the API, images + video thumbnails. |
| `x_tools.py` | `XToolbox`: the model's X tools (`x_thread_fetch`, `x_keyword_search`, `x_user_lookup`, `view_image`). Never raises; screens fetched posts for injection. |
| `safety.py` | Anti-scam URL screening, prompt-injection detection, output scanning, censorship detection. Pure functions, no I/O. |
| `contested_facts.json` | **Editorial.** The team's sourced framing for contested topics (today: COVID-19 origin). Injected as SOURCED POSITIONS when a question matches; its positions count as evidence for the gate. Every position needs a source and a date; the bot warns at startup when an entry's review is over 120 days old. |
| `venice_knowledge.py` | FAQ snapshot loader + keyword relevance ("crude RAG") and the live models API (6h TTL, snapshot fallback). |
| `twitter_client.py` | Thin tweepy wrappers: `get_mentions` (user context, for Owned Read pricing), `reply_to_tweet`, `get_tweet_by_id`, search, user lookup; `fit_x_rules` (one cashtag, @mentions only of thread participants). |
| `clients.py` | Constructs the authenticated tweepy client. |
| `image_processor.py` | Downloads the first photo attached to a tweet. |
| `state.py` | `State`: persisted set of processed tweet IDs + per-conversation author locks. |
| `utils.py` | URL extraction helpers. |
| `scripts/fetch_venice_data.py` | Refreshes `venice_faqs.json` + `venice_models.json` snapshots. Run manually or via cron. |
| `scripts/eval_replies.py` | Live probe: sample threads through the real Venice pipeline with X faked. |
| `reply_to_tweet.py`, `mock_test.py`, `test_local.py` | Manual / offline test harnesses (see below). |

Generated/data files: `state.json` (runtime), `venice_faqs.json` &
`venice_models.json` (committed snapshots, regenerated by the fetch script).

---

## Running & testing

```bash
python3.11 -m venv venv && source venv/bin/activate   # needs 3.10+
pip install -r requirements-dev.txt
cp env_example.txt .env        # fill in keys before running anything live

python -m pytest                       # offline suite — no API keys needed, run this first
python test_local.py --offline-only    # offline safety checks
python -m pytest -m live               # live Venice tests (VENICE_API_KEY only)
python scripts/eval_replies.py         # sample threads through live Venice, X faked
python reply_to_tweet.py <url> --dry   # the full pipeline on one real tweet, X keys needed
DRY_RUN=true python main.py            # the live loop, logging replies instead of posting
python main.py                         # run the live bot loop
```

**Always run `python -m pytest` and `python test_local.py --offline-only` after
touching `safety.py`, `config.py` prompts, `venice_api.py`, `x_context.py` or
`x_tools.py`.** They validate the scam/injection defences, routing, the thread
builder and the tool loop without needing credentials.

---

## Conventions

- **Python 3.10+** (uses `str | None`, `dict[str, int]` syntax). No type checker
  or linter is configured; match the surrounding style.
- **Standard library + `tweepy` + `requests` + `python-dotenv` only.** Don't add
  dependencies without a clear reason — keep `requirements.txt` minimal.
- All configuration lives in `config.py` as `Config` class attributes. Read
  settings via `Config.X`; don't scatter `os.getenv` calls elsewhere.
- Logging via the stdlib `logging` module (`logger = logging.getLogger(__name__)`).
  No `print` in the bot path — `print` is fine only in the manual test scripts.
- `safety.py` and `venice_knowledge.py` relevance functions are **pure** (no
  network, no global mutation beyond caches). Keep them that way so they stay
  testable offline.

---

## Things that will bite you

- **Model IDs are hardcoded** in `config.py` (`MODEL_UNBIASED`, `MODEL_COMPLEX`,
  …). Startup validation logs CRITICAL if the unbiased model disappears, ERROR
  for the others (plan 0001). Keep `venice_models.json` fresh so the check
  still works when the live fetch fails.
- **The uncensored model is abliterated.** It barely refuses, so the prompt's
  HARD LINES are backed in code: the router declines doxxing requests before
  any model runs, and `reply_gate.py` checks every final reply. Don't remove
  either without replacing it, and don't catch `ClefUnavailable` anywhere but
  `bot.py`: when Clef is down the mention must wait, never post unrouted or
  unchecked. `is_censored()` drives fallbacks: direct refusals
  count anywhere, hedges only when they are the whole reply; if every model
  refuses, nothing posts.
- **Venice skips web search** when the last user message holds an image or when
  scraping finds a URL in it. Images ride in their own user message ahead of the
  text (`_call_venice(image_parts=…)`), and `_venice_params` keeps scraping off;
  don't turn it back on. Venice also drops unknown `venice_parameters` keys
  without an error (the old `web_search_urls` never did anything), so check the
  docs before adding one.
- **Only `Config.reply_target()` belongs in prompts and length checks.**
  `char_limit()` is what X accepts (25,000 with Premium); when that number
  reached the prompt, replies ballooned. `craft_tweet()` and `rewrite_reply()`
  return nothing over the target, so an over-long reply is rewritten or dropped,
  never posted.
- **Every Venice call states today's date** (`_date_context()` in
  `_call_venice`). The bot sends `include_venice_system_prompt: false`, so
  nothing else tells the model the date, and without it the model treats its
  training cutoff as now and news it hasn't seen as fake.
- **`contested_facts.json` decides how the bot leads on its topics.** It is an editorial
  choice, so keep it to positions you can source and date, never state more than the
  sources do (the COVID entry says 'most likely', not 'confirmed'), and re-run
  `scripts/eval_replies.py --repeat 3 lab-leak covid-origin` after editing it.
- **X self-serve posting rules** reject replies the bot wasn't summoned to,
  posts with more than one cashtag, and @mentions of people outside the
  conversation. `fit_x_rules` handles the last two; keep it on the post path.
  A post with a URL costs $0.20 instead of $0.01, which is why the prompt keeps
  links out.
- **Kimi K3 defaults to `max` reasoning effort.** `REASONING_EFFORT_COMPLEX`
  sets it; dropping that line makes the complex lane much slower and pricier.
- **X tools depend on the account's X API plan.** Recent search and user lookup
  can 403; the toolbox then reports the tool as unavailable for the rest of
  that reply. Every X read (thread walk hops, tool results) costs on
  pay-per-use plans.
- **Several Venice calls per reply.** The Clef route, then `analyse()` (one call
  per tool round, up to `X_TOOL_MAX_ROUNDS + 1`), then `craft_tweet()` on the
  rewrite path, then the Clef gate. Web search ($0.01) can bill on every
  analysis call. `analyse()` usually emits a `[FINAL_REPLY]` block that
  `craft_tweet()` uses directly. Cost/latency live here.
- **`state.json` is committed to git despite being in `.gitignore`** (it was
  tracked before the ignore rule). Running the bot dirties the working tree.
  Don't commit churn in this file; see the plan to untrack it.
- **`state.save()` runs every loop iteration** and the processed-ID set is never
  pruned — it grows unbounded over time.
- **Mentions are fetched 5 at a time** (`MAX_MENTIONS_PER_CHECK = 5`, Twitter's
  minimum). A burst of mentions can exceed one page and get missed.
- Snapshot JSON files (`venice_faqs.json`, `venice_models.json`) are large.
  Regenerate them only with `scripts/fetch_venice_data.py` so diffs stay
  reviewable; don't hand-edit.

---

## Workflow: commits & PRs

This repo uses a feature-branch + PR workflow (see git history: `fix/…` branches
merged via PR).

- **Never commit directly to `main`.** Branch first: `fix/…`, `feat/…`, `docs/…`,
  `refactor/…`.
- **Conventional Commit** subjects: `fix:`, `feat:`, `refactor:`, `docs:`,
  `chore:`. Imperative mood, lower-case, no trailing period.
- Keep commits focused — one logical change each. Explain the *why* in the body
  when it isn't obvious from the diff.
- **The user decides what to stage and commit.** Propose a commit message; don't
  run `git commit`/`git push` unless explicitly asked.
- Plans and the reasoning behind larger changes go in `docs/` (see
  `docs/plans/`). Write the plan before the big change.
- Don't commit secrets (`.env`) or runtime churn (`state.json`).
