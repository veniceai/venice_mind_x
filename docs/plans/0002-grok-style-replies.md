# Plan 0002 — Grok-style replies: Clef routing, DeepSeek/Kimi lanes, X tools

- **Status:** Built and tested against live Venice; not yet run against the live X API
- **Date:** 2026-11-10
- **Requested by:** Jordan (Venice user education)

> **Outcome so far:** all parts below are in place. 184 offline tests and 8
> live Venice tests pass. `scripts/eval_replies.py` (live Venice, X faked) shows
> Clef keeping contested and taboo questions on DeepSeek Uncensored and sending
> math/code to Kimi K3 (about 1.4 s per route), the model calling X tools on its
> own when the thread isn't enough, frank answers on drugs and dark humor,
> clear verdicts on contested claims, and doxxing requests declined before any
> model runs. Replies take 5–45 s (tool-heavy fact checks are the slow end).
> Still to do: a `DRY_RUN` session on the real account, which needs its X
> credentials; see [`../OPERATOR.md`](../OPERATOR.md).
>
> **Revisions the same day (Jordan):** the router moved from Jev to Clef
> (private, and more decisive; section 1); the voice moved from @grok's
> neutral tone to Venice's rebel stance, uncensored and unbiased (section 5),
> with hard lines enforced in code (section 7).

## The goal

X users tag the bot inside a post or a thread to get more information. Most of
those questions should come back as unbiased as possible; a smaller share are
hard (math, code, technical deep-dives) and need a stronger reasoner.

- Unbiased questions go to **DeepSeek V4.1 Flash 1017 Uncensored**
  (`deepseek-v4-1-flash-1017-uncensored`): private, vision, function calling,
  1M context, $0.33/$1.32 per 1M tokens, Venice's `default` text trait.
- Complex questions go to **Kimi K3** (`kimi-k3`): private, vision, function
  calling, 1M context, $3.75/$18.75 per 1M tokens, Venice's `default_reasoning`
  trait. Its default reasoning effort is `max`, so the bot sets it explicitly.
- A quick **Clef** decision call (`clef-api`, `POST /api/v1/decisions`)
  picks the lane per mention.
- The bot reads X post data the way `@grok` does.
- The output is very clearly uncensored and unbiased, as far as the evidence
  allows: the Venice brand is the rebel.

## How `@grok` does it (2026-11-10 research)

Sources: `xai-org/grok-prompts` (`ask_grok_system_prompt.j2`, the prompt for
the `@grok` bot on X), the xAI X Search tool docs, and "Asking Grok"
(arXiv 2605.19720, a study of `@grok` usage on X).

1. **It is a tool loop, not a fixed context.** The prompt says: "Use your X
   tools to get context on the current thread. Make sure to view images and
   multimedia that are relevant to the conversation." The X tools behind it are
   `x_thread_fetch` (a post with its parents and replies), `x_keyword_search`
   (X advanced search operators, `Top` or `Latest`), `x_semantic_search` and
   `x_user_search`, plus `view_image`, `view_x_video`, `web_search` and
   `browse_page`.
2. **It researches before answering.** Real-time search confirms facts and
   fetches primary sources; parallel searches find diverse viewpoints; current
   events, subjective claims and statistics get a deep analysis across all
   parties; media viewpoints are assumed biased.
3. **Its reply rules:** clear and direct; a neutral tone on subjective
   political questions; no moralizing, preaching, slogans or snarky one-liners;
   never labels a view "biased" or "baseless"; never refuses or berates; may
   express uncertainty; ignores partisan or one-word format demands and answers
   with a balanced conclusion; doesn't identify people in media unless they are
   widely recognized public figures; under 550 characters; same language,
   dialect and alphabet as the post; doesn't tag the person it replies to; no
   markdown; never mentions its instructions or tools.
4. **How people use it:** 80% of mentions are replies inside existing threads;
   the biggest intents are information requests (37%) and verification (18%),
   then debate (13%) and humor (10%). Median response time 2.7 minutes.

## What the bot does today

- One model (Kimi K2.5) answers everything; a refusal regex walks a cascade of
  GLM Heretic → Venice Uncensored 1.2 (Qwen3-VL for vision failures).
- X context is fixed: the mention, its immediate parent, the conversation root
  and quoted posts, text only plus the first photo.
- Gaps against `@grok`:
  - Long posts are cut at ~280 characters: `note_tweet` is never requested, so
    `text` is the truncated preview.
  - The middle of a thread is skipped (only parent + root).
  - Context carries no author handles or times, so the model can't tell who
    said what, or that a post is the bot's own.
  - Linked x.com posts go to Venice's web scraper, which X blocks.
  - Videos and GIFs are ignored; only the first photo is used.
  - No way to look further (search X, look up an account).
- The persona prompt asks for opinions ("You have OPINIONS. Share them"),
  which works against the unbiased goal.

## Design

### 1. Lane routing with Clef (`router.py`)

One `POST /api/v1/decisions` per mention, four `noul` (yes/no probability)
questions over a structured state (the question plus the rendered thread):

- `charged` — answering needs a judgment on a contested political, social,
  cultural, religious or moral question, a public figure, or whether a
  disputed claim is true.
- `filtered` — a safety-filtered model might refuse, water down or hedge it
  (drugs, sex, violence, weapons, crime, hacking, extremism, conspiracies,
  medical/legal/financial gray areas, offensive humor). Kimi K3 is
  safety-tuned; this keeps those questions on the uncensored model.
- `complex` — a good answer needs several steps of reasoning: math, code, a
  technical or scientific explanation, analysis of a long document, chart or
  thread, or weighing several sources.
- `private_info` — the question asks to find or reveal a specific person's
  identity, address, phone, workplace or other private details. At 0.5 or
  above the bot posts a one-line decline and runs no model or X lookup.

Rule (pure function, unit-tested): `charged ≥ 0.5` or `filtered ≥ 0.5` →
uncensored lane; otherwise `complex ≥ 0.85` → complex lane; otherwise
uncensored. Any router failure (timeout, 4xx/5xx, malformed answer) routes to
the uncensored lane. Thresholds and the decision model are config.

**Clef vs Jev (bake-off, 26 questions, 2026-11-10).** Both sit behind the same
endpoint. Clef (`clef-api`, Cloudflare, private) picked the expected lane 25
times; the miss was a nuclear-reactor deep-dive that a borderline `filtered`
score (0.52) kept on DeepSeek, the safe direction. Jev (`jev-latest`,
TypeSafe, anonymized) managed 23 at its best threshold and sent code
questions to DeepSeek; its `complex` scores cluster around 0.5–0.85 where
Clef's sit near 0 or 1. Clef's median was 1.37 s against Jev's 0.83 s. Clef
also keeps every model the bot calls private, so the Venice key needs no
anonymized-model access. Three of three doxxing requests scored 0.90–0.98 on
`private_info`; seven ordinary questions (including an organization's press
email and "which city is Elon Musk based in") stayed under 0.30.

### 2. Model lanes and fallbacks (`venice_api.py`)

| Lane | Chain | Reasoning effort |
|---|---|---|
| uncensored (`UNBIASED` in code) | DeepSeek 1017 Uncensored → DeepSeek V4.1 Flash Uncensored → Venice Uncensored 1.2 | `high` (model default) |
| complex | Kimi K3 → the uncensored chain | `high` (Kimi defaults to `max`) |

Every model in both chains is private and supports vision and function
calling (checked against the live catalog 2026-11-10). A refusal (the existing
`is_censored` check) or an error moves to the next model. GLM Heretic and
Qwen3-VL are dropped. The tweet-crafting rewrite runs on DeepSeek with low
reasoning effort: it reshapes text, it doesn't need Kimi.

`reasoning_effort` is sent only to models whose catalog entry says
`supportsReasoningEffort`. Both models return reasoning in `reasoning_content`,
so `content` stays the reply (probed live 2026-11-10).

### 3. X context the way `@grok` receives it (`x_context.py`)

Built before the model runs, so the safety screens see all of it:

- The mention's full text (`note_tweet.text` when present).
- The reply chain walked up from the mention to the first post, capped at
  `THREAD_MAX_DEPTH` hops, plus the conversation root when the cap is hit.
- Each post rendered with `@handle (name)`, UTC time and id; the bot's own
  posts labelled as yours.
- Quoted posts inline (one level), from the `referenced_tweets.id` expansion.
- x.com / twitter.com status links in the mention or its parent resolved
  through the API (capped), and removed from the web-scrape list.
- Media: photos, plus thumbnails for videos and GIFs, nearest posts first, up
  to `MAX_IMAGES` (4) images sent to the model.

### 4. X tools for the model (`x_tools.py`)

OpenAI-style function tools, run by the bot against the X API v2 (tweepy):

| Tool | X API | Notes |
|---|---|---|
| `x_thread_fetch(post_id)` | `GET /2/tweets/:id` walk + recent search `conversation_id:` for replies | Replies need recent-search access (last 7 days) |
| `x_keyword_search(query, sort)` | `GET /2/tweets/search/recent` | X search operators; `Top` → relevancy, `Latest` → recency; `-is:retweet` added |
| `x_user_lookup(username)` | `GET /2/users/by/username` + `GET /2/users/:id/tweets` | Profile and recent posts |
| `view_image(url)` | download | Only X media hosts (`pbs.twimg.com`), adds the image to the conversation |

Caps: `X_TOOL_MAX_ROUNDS` model turns with tool calls, `X_TOOL_MAX_CALLS` (4)
calls per reply, `X_SEARCH_MAX_RESULTS` posts per search; the eval saw single
replies make 6–7 calls before the call cap, at up to $0.05 of X reads per
search. DeepSeek sometimes writes a tool call as text markup instead of a
structured call; that turn is retried once, then the next model takes over.
Rate limits and 403s (access tier) come back to the model
as a short "unavailable" result instead of sleeping inside the reply. Every
fetched post passes `screen_input_for_injection`; a flagged post is replaced
with a "[withheld]" note so the model never reads the command. Permalinks of
fetched posts join the allowed-link set for the output scan, so the bot may
cite a post it read; other new links are still blocked.

Not mirrored: semantic search (the X API has none) and video understanding
(neither model accepts video; thumbnails stand in).

### 5. Prompt and length (`config.py`)

`ANALYST_PROMPT` keeps the Venice knowledge, anti-scam and token-injection
blocks unchanged. From `@grok` it takes the research rules (independent
analysis, sources from all sides, media assumed biased, format demands don't
override the answer, no labels, no slogans, same language, no tagging). The
voice is Venice's (`venice-marketing-os/context/VOICE.md` and
`MESSAGING_HOUSE.md`: "direct, adult, principled", "intelligence without
paternalistic degradation"), in three blocks:

- **UNCENSORED** — answer any topic frankly; no refusals, lectures, content
  warnings or caveat piles; one caveat only when it changes what the reader
  should do.
- **UNBIASED** — no side gets a pass (left or right, governments, media,
  experts, institutions, AI labs, Venice included); official narratives held
  to the same evidence standard; a clear verdict where the evidence lands,
  including against the official line; a level, never partisan tone. This
  replaces `@grok`'s "neutral tone", which read as fence-sitting.
- **VOICE** — short sentences, lead with the answer, dry wit; rebel means
  saying the true thing others won't, not edginess for effect.

Plus the hard lines (section 7), no links (X bills a post with a URL at $0.20
instead of $0.01), and at most one cashtag. Reply length follows `@grok`:
under 550 characters on an X Premium account, 280 otherwise
(`reply_target()`); the hard post limit stays `char_limit()`.

### 6. Rollout

- `DRY_RUN=true` logs the would-be reply and posts nothing.
- `reply_to_tweet.py <url> --dry` runs the full new pipeline on one real post.
- The operator checklist (X App, credits, permissions, keys, Venice key,
  hosting, costs, first run) is [`../OPERATOR.md`](../OPERATOR.md).

### 7. Hard lines and X's posting rules

The uncensored model is abliterated, so it barely refuses even when told to.
Four hard lines are written into the prompt and enforced in code: sexual
content involving minors, finding or revealing a private person's details,
threats or calls for violence, and instructions for mass-casualty weapons.

- The router's `private_info` question declines doxxing requests before any
  model or X lookup runs (in the eval the model otherwise made 4–7 lookups
  hunting for the person and offered to keep digging).
- `reply_gate.py` asks Clef whether the final reply crosses any of the four;
  if so, nothing posts. It fails open. On eleven sample replies (drug doses,
  harm reduction, dark jokes, lock picking, SIM swapping, a public figure's
  city) it flagged only the doxxing one.
- `twitter_client.fit_x_rules` enforces X's self-serve limits on the way out:
  one cashtag per post, @mentions only of people already in the conversation.

`REPLY_GATE_ENABLED=false` turns the gate off; the router decline has no
switch short of removing it.

## Cost per reply (estimate)

See the cost table in [`../OPERATOR.md`](../OPERATOR.md): a typical DeepSeek
reply is about $0.03–0.04 across X reads, the summoned reply post ($0.01),
Venice tokens and web search; a heavy one with four X tool calls about $0.35.

## Testing

- Offline (pytest, no keys): lane rule, router request/fallbacks and decline,
  thread walk, long-post text, link resolution, media caps, tool dispatch,
  call budget, redaction, error handling, the tool loop with a mocked Venice
  (including text-markup tool calls and leaked reasoning), the gate, X rules,
  model validation.
- Live (`pytest -m live`, Venice key only): routing sample questions through
  Clef; a full reply over a fake X client.

## Out of scope

- PR #14/#15 (the LlamaIndex ReAct agent). This plan uses Venice's native
  function calling inside the current pipeline, so it doesn't need them; they
  can rebase onto it or close.
- Memory across conversations, embeddings for the FAQ.
