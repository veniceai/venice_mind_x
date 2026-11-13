# Plan 0003 — Plain-language replies (ELINA)

- **Status:** Built; 11 scenarios run against live Venice before and after
- **Date:** 2026-11-13
- **Requested by:** Jordan (Venice user education)

## The goal

Replies should be clear and easy to read for someone who isn't an expert in the
topic: ELINA ("explain like I'm not an expert"). That means adult and plain, with
no jargon and no talking down. Jordan considered ELI15 and settled on ELINA.

## What the replies looked like (baseline)

`scripts/eval_replies.py factcheck one-word math code venice x-tools longpost
portuguese psilocybin lab-leak mdma`, on `feat/grok-style-replies` at 290bffa.
The verdicts and facts were good; the writing was hard to follow:

- **Two sentences carrying four or five facts**, chained with colons and
  semicolons, at 255–275 of the 280 characters. The length rule in `analyse()`
  asked for "one or two sentences", so the model compressed instead of cutting.
  "EUR 410M tender became EUR 1.2B: steel tariffs added ~38% to input costs,
  softer riverbed than surveyed (two extra piers, 14 months), and a mid-build
  tram lane."
- **Jargon nobody explained:** "hits 5-HT2A receptors, quiets the default mode
  network and loosens thalamic gating", "testing a pinch misses the
  chocolate-chip effect", "mint against your locked sVVV".
- **Author surnames and notation:** "Berkeley's Reich and Sosinskiy … Clemens
  and Wither at 2.3 to 3.9 percent", "365!/(335!·365^30)".
- **Em dashes and `~`/`≈` shorthand.**

## Changes

- **`ANALYST_PROMPT`:** a PLAIN LANGUAGE block. Write for a smart adult outside
  the field; everyday words; explain a technical term the reader needs, otherwise
  say what the thing does; one idea per short, complete sentence; one to three
  facts, cut a fact rather than squeeze the wording; round numbers; name sources
  in plain terms (a Berkeley study, the CIA), not author surnames; no em dashes.
  One dense-versus-plain example, on a topic outside the eval (the Great Wall
  from space) so the eval stays a fair test.
- **DEPTH SCALING:** a fact check takes the core claim (two at most) with the one
  or two facts that decide it. Math and code give the answer and the reason in
  plain words, and show working only when asked, as short lines.
- **`CRAFTER_PROMPT`:** the same rules for the rewrite path.
- **`analyse()` length rule:** "two to four short sentences", and "drop a fact
  instead of squeezing the wording". The output template asks for plain language.
- **Hard line fix:** the doxxing line ended mid-sentence ("or offering to look");
  it now ends "them up".
- **Eval checks:** psilocybin accepts spelled-out units ("3 grams"); math accepts
  the rounded "71%"; Portuguese no longer flags a reply that denies the overclaim
  ("não significa só nos servidores da Venice"), which failed in both runs.

## Results (same 11 scenarios)

| | Before | After |
|---|---|---|
| Average words per sentence | 14.4 | 11.2 |
| Sentences over 20 words | 8 | 2 |
| Em dashes | 4 | 0 |
| `~` / `≈` | 3 | 0 |
| Colons and semicolons | 13 | 8 |
| Average length (characters) | 256 | 250 |

Verdicts and the facts each check looks for held in every scenario. Two pairs:

- Code. Before: "Deadlock: threading.Lock isn't re-entrant. f() acquires the lock, then calls
  g(), which tries to acquire the same lock in the same thread — it blocks
  waiting for itself, forever." After: "Deadlock. threading.Lock can't be taken
  twice, even by the same thread. f grabs the lock, then calls g, and g waits
  forever for a lock that f is already holding. The fix is threading.RLock…"
- Drug checking. Before: "testing a pinch misses the chocolate-chip effect".
  After: "Testing all of it beats a pinch, since fentanyl hides in one crumb."

## Still to watch

- Replies still fill most of the 280 characters; they are longer in sentences,
  not shorter overall.
- The DIEM answer still says "mint", "sVVV" and "DEX", and "that credit renews
  daily, and it never expires" can be read as unused credit carrying over, which
  it doesn't.
- The Premium target (550 characters) wasn't tested; `.env` sets
  `X_PREMIUM_ENABLED` twice and the later `false` wins.
- Unchanged by this plan: the one-word capitalism question still opens "Good."
