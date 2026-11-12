# Running @venice_mind: what the operator needs

Everything the person who holds the bot's credentials has to have in place,
checked against X's and Venice's docs on 2026-11-10. Work through it top to
bottom; the first-run steps at the end confirm each part.

## 1. X developer setup

- [ ] **A developer account, Project and App** at [console.x.com](https://console.x.com).
  Create the App under the **@venice_mind** account itself if you can: X bills
  an account's own mentions as Owned Reads ($0.001 per post instead of $0.005)
  only when the authenticated user owns the developer App.
- [ ] **Pay-per-use credits** bought in the Developer Console, with a spending
  limit and a balance alert or auto-recharge. When credits run out, X API
  requests fail and the bot goes quiet.
- [ ] **App permissions: Read and write.** Generate (or regenerate) the Access
  Token and Secret **after** setting Read and write; tokens made under
  Read-only can't post and every reply fails with a 403.
- [ ] **Five keys** in `.env`:

  | Variable | What it is |
  |---|---|
  | `TWITTER_API_KEY`, `TWITTER_API_SECRET` | The App's consumer key and secret |
  | `TWITTER_ACCESS_TOKEN`, `TWITTER_ACCESS_TOKEN_SECRET` | OAuth 1.0a user tokens for @venice_mind |
  | `TWITTER_BEARER_TOKEN` | The App's bearer token (app-only reads) |

- [ ] **Premium:** if @venice_mind has X Premium, set `X_PREMIUM_ENABLED=true`
  so replies aim for 550 characters (like @grok) instead of 280.
- [ ] Recommended: label @venice_mind as an automated account in its X settings,
  managed by @AskVenice.

### Endpoints the bot calls

| Endpoint | Used for | Auth |
|---|---|---|
| `GET /2/users/me` | Startup: the bot's id and handle | user |
| `GET /2/users/:id/mentions` | Polling mentions every 3 minutes | user (Owned Reads) |
| `GET /2/tweets/:id` | Walking the thread, quoted and linked posts | bearer |
| `POST /2/tweets` | Posting the reply | user |
| `GET /2/tweets/search/recent` | `x_keyword_search`, replies in `x_thread_fetch` | bearer |
| `GET /2/users/by/username/:username`, `GET /2/users/:id/tweets` | `x_user_lookup` | bearer |

If the plan rejects search or user lookup, the bot keeps replying; the model
is told those tools are unavailable.

### X's posting rules for self-serve apps

- **Replies only where summoned.** The API accepts a reply only if the post
  being replied to @mentions the bot or quotes one of its posts. The bot only
  answers its mentions, so this holds; it can't reply into threads it wasn't
  tagged in.
- **One cashtag per post.** More than one `$TICKER` and X rejects the post.
  `twitter_client.fit_x_rules` strips the `$` from any extra cashtags.
- **@mentions only of people in the conversation.** `fit_x_rules` drops the @
  from any other handle.
- **Volume:** 300 posts per 3 hours per account; `POST /2/tweets` 100 per 15
  minutes per user. The bot's own cap is 15 replies an hour.

## 2. Venice

- [ ] **`VENICE_API_KEY`**: an `INFERENCE` key on an account with credits (USD
  or DIEM), ideally with a consumption limit set on the key.
- [ ] The key can call these models (all private, so a key limited to private
  models works):

  | Model | Role |
  |---|---|
  | `deepseek-v4-1-flash-1017-uncensored` | Uncensored, unbiased lane (default), tweet rewrite |
  | `kimi-k3` | Complex lane |
  | `deepseek-v4-1-flash-uncensored` | Fallback |
  | `venice-uncensored-1-2` | Last resort |
  | `clef-api` | Router and final-reply gate, via `POST /api/v1/decisions` (Beta) |

  At startup the bot logs CRITICAL if the main model is missing and ERROR for
  the others.

## 3. Where it runs

- [ ] **Python 3.10 or newer** (tested on 3.11); `pip install -r requirements.txt`.
- [ ] **One long-running process**, `python main.py`, under a supervisor
  (systemd, pm2, a container) that restarts it. Run exactly **one** instance:
  two would answer every mention twice.
- [ ] A **writable working directory** for `state.json` (answered mentions and
  conversation locks).
- [ ] **Outbound HTTPS** to `api.venice.ai`, `api.twitter.com` (tweepy's host
  for the X API) and `pbs.twimg.com` (post images).

### Switches (`.env`)

| Variable | Default | Effect |
|---|---|---|
| `DRY_RUN` | false | Log replies, post nothing |
| `ROUTER_ENABLED` | true | false: every mention goes to DeepSeek |
| `REPLY_GATE_ENABLED` | true | false: skip the hard-line check on the final reply |
| `X_PREMIUM_ENABLED` | false | true: 550-character replies |

To pause the bot without a restart, create a file named `PAUSED` in its
working directory (`touch PAUSED`). It stops reading mentions on its next loop,
within a few minutes, and resumes when the file is deleted. Mentions older than
60 minutes by then are skipped.

## 4. What a reply costs

From the X rate card and Venice's pricing page, 2026-11-10.

| Item | Unit price | Typical reply | Heavy reply |
|---|---|---|---|
| X: the mention (Owned Read) | $0.001 per post | $0.001 | $0.001 |
| X: thread walk | $0.005 per post, once a day per post | $0.005–0.015 | $0.035 |
| X: tool calls (max 4) | search up to 10 posts × $0.005; user lookup $0.010 + 5 posts | $0 | $0.20 |
| X: the reply | $0.010 summoned ($0.200 with a URL; the prompt keeps links out) | $0.010 | $0.010 |
| Venice: Clef route + gate | $0.05 per 1M tokens | under $0.001 | under $0.001 |
| Venice: DeepSeek lane | $0.33 / $1.32 per 1M tokens, 1–4 calls | $0.005 | $0.02 |
| Venice: Kimi K3 lane (math, code) | $3.75 / $18.75 per 1M tokens | $0.03–0.08 | $0.30 |
| Venice: web search | $0.01 per search, per analysis call | $0.01 | $0.04 |

A typical DeepSeek reply comes to about $0.03–0.04; a heavy one with four X tool
calls to about $0.35. At the 15-replies-an-hour cap that bounds a busy day
near $10–15, with a worst-case ceiling around $125. Set both spending limits.

## 5. First run

1. `python -m pytest` — the offline suite, no keys needed.
2. `python reply_to_tweet.py <post url> --dry` on three to five real threads
   that tag @venice_mind. This checks the keys, the thread walk and search
   access, and prints the reply without posting.
3. `DRY_RUN=true python main.py` for an hour: mentions are polled and replies
   logged, nothing is posted.
4. `python main.py`.

### Log lines to watch

| Log line | Meaning |
|---|---|
| `CRITICAL … not in Venice's available model list` | A model ID was renamed or retired; fix `config.py` |
| `Venice API error … HTTP 402` | Venice credits are out |
| `Router (clef-api) failed` | Every mention falls back to DeepSeek until it recovers |
| `not available on the X API plan` | The X plan rejects search or user lookup |
| `403 on reply` | Read-only tokens, or a reply X's summon rule rejected |
| `Not posting: the reply fails the … check` / `Declining a request for someone's private details` | The gate or the router stopped a reply |
| `Reply gate failed` / `Router (clef-api) failed` (ERROR) | Clef is down: replies post unchecked and doxxing requests aren't declined until it recovers |
| `REPLY_GATE_ENABLED is off` / `ROUTER_ENABLED is off` (ERROR at startup) | A safety switch is off |
