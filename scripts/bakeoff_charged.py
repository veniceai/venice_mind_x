#!/usr/bin/env python3
"""Charged-lane bake-off: DeepSeek 1017 Uncensored vs Grok with native X search.

Runs the eval's charged scenarios through the full pipeline twice, swapping
only the analysis model of the uncensored lane; routing, the crafter (DeepSeek)
and the gate stay the same. Web scraping is off for both, as in the bot, so
link threads get web search. X is faked for the bot's own
X tools; Grok's native X search reads live X. Writes a markdown table.

    python scripts/bakeoff_charged.py --image photo.png --out bakeoff.md
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "scripts"), os.path.join(ROOT, "tests")]

import requests  # noqa: E402

import eval_replies as ev  # noqa: E402
import venice_api  # noqa: E402
from config import Config  # noqa: E402
from x_fakes import resp, tweet  # noqa: E402

CHARGED = ["factcheck", "one-word", "lab-leak", "gossip", "breaking", "death-hoax", "death-real",
           "x-tools", "image-search", "link-thread"]
# Grok gets xAI's native X search from venice_api (Config.NATIVE_X_SEARCH).
VARIANTS = ["deepseek-v4-1-flash-1017-uncensored", "grok-4-3"]

_real_post = requests.post
usage = []


def _post(url, json=None, **kw):
    r = _real_post(url, json=json, **kw)
    if json and "messages" in json:
        try:
            body = r.json()
            u = body.get("usage") or {}
            vp = body.get("venice_parameters") or {}
            usage.append({"model": json["model"], "in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0),
                          "cites": len(vp.get("web_search_citations") or [])})
        except Exception:
            pass
    return r


def link_thread():
    return ev._scenario(
        "link-thread",
        tweet(406, "@newsflash_now @venice_mind is this accurate?", 3, reply_to=405, conversation_id=405),
        {405: resp(tweet(405, "The Fed just cut rates by a full point at its last meeting https://t.co/fedcut1", 15,
                         urls=[{"url": "https://t.co/fedcut1", "expanded_url": "https://www.reuters.com/markets/us/fed-decision/"}]),
                   users=[ev.NEWS])},
        [ev.CAROL], note="claim with a link: search must still run",
        check=lambda r: ev._ok(r.web_citations > 0 and r.final, "web search ran on a link thread"),
    )


def price(model):
    from venice_knowledge import get_models
    for m in get_models():
        if m.get("id") == model:
            p = (m.get("model_spec") or {}).get("pricing") or {}
            return (p.get("input") or {}).get("usd") or 0, (p.get("output") or {}).get("usd") or 0
    return 0, 0


def main():
    args = sys.argv[1:]
    opts = {}
    for flag in ("--image", "--out"):
        if flag in args:
            i = args.index(flag)
            opts[flag] = args[i + 1]
            del args[i:i + 2]
    image_path = opts.get("--image")
    only = set(args)
    venice_api.requests.post = _post

    scenarios = {s.name: s for s in ev.scenarios()}
    scenarios["link-thread"] = link_thread()
    rows = []
    for name in CHARGED:
        sc = scenarios[name]
        if sc.needs_image and not image_path:
            continue
        for model in [m for m in VARIANTS if not only or m in only]:
            real_chain = venice_api._lane_chain
            # The lane's first model only; a failure must not hide behind a fallback.
            venice_api._lane_chain = lambda lane, m=model: [(m, Config.REASONING_EFFORT_UNBIASED)]
            usage.clear()
            t = time.time()
            try:
                r = ev.run(sc, image_path)
            finally:
                venice_api._lane_chain = real_chain
            ok, why = sc.check(r)
            cost = 0.0
            for u in usage:
                pin, pout = price(u["model"])
                cost += u["in"] / 1e6 * pin + u["out"] / 1e6 * pout
            cites = sum(u["cites"] for u in usage if u["model"] == model)
            shown = r.final if r.final else f"(not posted: {r.failed}) draft: {r.drafted}"
            print(f"\n=== {name} · {model}: {'PASS' if ok else 'FAIL'} ({why}) {time.time() - t:.0f}s "
                  f"${cost:.3f} cites={cites} lane={r.lane}\n  {shown}")
            rows.append((name, model, ok, why, time.time() - t, cost, cites, shown))

    lines = ["| Scenario | Model | Result | Time | Venice tokens | Citations | Reply |", "|---|---|---|---|---|---|---|"]
    for name, model, ok, why, secs, cost, cites, shown in rows:
        cell = shown.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {name} | {model} | {'PASS' if ok else '**FAIL**'} ({why}) | {secs:.0f}s | ${cost:.3f} | {cites} | {cell} |")
    summary = []
    for model in VARIANTS:
        mine = [r for r in rows if r[1] == model]
        if mine:
            summary.append(f"- `{model}`: {sum(r[2] for r in mine)}/{len(mine)} passed, median "
                           f"{sorted(r[4] for r in mine)[len(mine) // 2]:.0f}s, ${sum(r[5] for r in mine):.2f} in Venice tokens")
    text = "\n".join(lines) + "\n\n" + "\n".join(summary) + "\n"
    print("\n" + "\n".join(summary))
    if "--out" in opts:
        with open(opts["--out"], "w") as f:
            f.write(text)


if __name__ == "__main__":
    main()
