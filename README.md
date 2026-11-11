# Venice X Bot (@venice_mind)

An X reply bot powered by Venice AI. People tag it in a post or thread to get
more information. It reads X the way `@grok` does and answers in Venice's
voice: uncensored and unbiased, with every model in private mode.

Running it: [`docs/OPERATOR.md`](docs/OPERATOR.md) lists everything the
operator needs (X App and credits, keys, Venice key, hosting, costs, first run).

## Features

- **Two model lanes, routed per mention by Clef** (Venice's private decision
  model): DeepSeek V4.1 Flash 1017 Uncensored for anything contested or taboo
  (most questions), Kimi K3 only for math, code and technical deep-dives that
  aren't
- **Uncensored, unbiased voice** — frank answers on any topic, a clear verdict
  where the evidence lands (or Unverified), no side gets a pass; a final check on
  every reply for hard lines, unsupported claims about real people, and VVV/DIEM
  return figures; plus a short list of hard lines
  (minors, doxxing, threats, mass-casualty weapons) checked on every reply
- **Reads the whole thread** — every post in the reply chain with its author,
  long posts in full, quoted and linked posts, photos and video thumbnails
- **X tools** the model calls when the thread isn't enough: thread fetch,
  keyword search, account lookup, image viewing
- **Real-time web search** for prices, news, and current events
- **Anti-scam protection** — blocks phishing URLs, warns about lookalikes,
  withholds token-launch commands found in fetched posts

## Quick Start

```bash
cd twitter_bot_venice
python -m venv venv

# Activate the virtualenv:
source venv/bin/activate        # Linux / macOS
# venv\Scripts\activate         # Windows (PowerShell/cmd)

pip install -r requirements.txt
cp env_example.txt .env   # then edit .env and fill in your keys
python main.py
```

> Requires Python 3.10+. Without valid keys in `.env`, `python main.py` exits
> immediately with a clear `CRITICAL | Fatal: 401 Unauthorized` — that's
> expected. To check the install without keys, run the offline tests below.

## Testing

```bash
# Run offline tests
python test_local.py --offline-only

# Test a specific tweet (asks before posting; --dry just prints)
python reply_to_tweet.py https://x.com/user/status/1234567890 --dry

# Sample threads through the live Venice pipeline, X faked (no X keys, posts nothing)
python scripts/eval_replies.py

# Run the live bot without posting: replies go to the log
DRY_RUN=true python main.py
```

## Refreshing Venice knowledge

The bot answers Venice questions from a committed FAQ snapshot and the live
models API. Re-sync the snapshots whenever Venice updates its FAQ or models:

```bash
python scripts/fetch_venice_data.py   # rewrites venice_faqs.json + venice_models.json
```

Run it manually or via cron. Model data is also fetched live at runtime (6h
cache); the snapshot is the offline fallback. Commit the updated JSON files.

## Project Structure

```
twitter_bot_venice/
├── main.py              # Entry point
├── bot.py               # Mention polling + processing
├── config.py            # Models, prompts, settings
├── router.py            # Clef lane routing
├── reply_gate.py        # Clef check on the final reply (hard lines, facts, token returns)
├── finalize.py          # X rules + token backstop + gate, one rewrite
├── venice_api.py        # Venice API calls, tool loop, model cascade
├── x_context.py         # The thread a mention gets (chain, quotes, links, media)
├── x_tools.py           # X tools the model can call
├── safety.py            # Anti-scam URL screening
├── twitter_client.py    # Twitter API wrappers
├── state.py             # Processed-tweet persistence
└── test_local.py        # Test suite
```

## Models

All private, all with vision and function calling. Prices per 1M input/output
tokens, from the Venice catalog on 2026-11-10.

| Model | Role | Price |
|-------|------|-------|
| `deepseek-v4-1-flash-1017-uncensored` | Unbiased lane (default), tweet rewrite | $0.33 / $1.32 |
| `kimi-k3` | Complex lane (reasoning effort `high`) | $3.75 / $18.75 |
| `deepseek-v4-1-flash-uncensored` | Fallback | $0.33 / $1.32 |
| `venice-uncensored-1-2` | Last resort | $0.20 / $0.90 |
| `clef-api` | Router and final-reply gate (private decision model) | $0.05 / $0.05 |

Clef asks four yes/no questions per mention: is it charged (contested,
political, a disputed claim), is it a topic a filtered model would dodge (drugs,
sex, crime, hacking, conspiracies…), is it complex (multi-step reasoning), and
does it ask to dig up someone's private details. Charged or filtered questions
always go to DeepSeek Uncensored; complex ones that are neither go to Kimi K3;
anything else, and any router failure, goes to DeepSeek. Doxxing requests get a
one-line decline. On a 26-question sample Clef picked the expected lane 25
times (Jev, the anonymized alternative, 23). Design and research:
[`docs/plans/0002-grok-style-replies.md`](docs/plans/0002-grok-style-replies.md).

## X API access

The bot reads mentions and posts replies, and walks threads with post lookups.
The model's X tools also use recent search (`x_keyword_search`, replies in
`x_thread_fetch`) and user lookup (`x_user_lookup`). If the account's X API
plan doesn't include one of those, the tool tells the model it's unavailable
and the reply goes ahead without it.

## Configuration

Key settings in `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `MAX_TWEET_AGE_MINUTES` | 60 | Ignore older tweets |
| `MAX_REPLIES_PER_HOUR` | 15 | Rate limit |
| `X_PREMIUM_ENABLED` | false | Replies aim for 550 chars (like `@grok`) if true, 280 if false |
| `ROUTER_COMPLEX_THRESHOLD` | 0.85 | Clef's "complex" probability that sends a question to Kimi K3 |
| `ROUTER_FILTERED_THRESHOLD` | 0.5 | Clef's "a filtered model would dodge this" probability that keeps it on DeepSeek |
| `X_TOOL_MAX_CALLS` | 4 | X tool calls per reply (each search reads up to 10 posts at $0.005) |
| `THREAD_MAX_DEPTH` | 6 | Reply-chain posts walked up from a mention |
| `X_TOOL_MAX_ROUNDS` | 3 | Model turns per reply that may call X tools |

## Security

The bot includes multi-layer anti-scam protection:

1. **URL screening** — classifies URLs as trusted/suspicious/blocked
2. **Selective scraping** — only trusted domains are scraped
3. **Prompt hardening** — AI knows only venice.ai is official
4. **Output scanning** — blocks replies that endorse scam URLs

### Trusted Domains

Only these are treated as official Venice:
- `venice.ai` and subdomains
- Official social accounts
- Blockchain explorers (etherscan, basescan, etc.)

### Adding Scam Domains

Edit `safety.py`:
- `BLOCKLISTED_DOMAINS` — known scam domains
- `_SCAM_URL_PATTERNS` — regex patterns (claim, airdrop, etc.)

## Environment Variables

```
TWITTER_BEARER_TOKEN=
TWITTER_API_KEY=
TWITTER_API_SECRET=
TWITTER_ACCESS_TOKEN=
TWITTER_ACCESS_TOKEN_SECRET=
VENICE_API_KEY=          # INFERENCE key; every model the bot calls is private
X_PREMIUM_ENABLED=false
DRY_RUN=false            # true: log replies, post nothing
ROUTER_ENABLED=true      # false: every mention goes to DeepSeek
REPLY_GATE_ENABLED=true  # false: skip the hard-line check on the final reply
```
