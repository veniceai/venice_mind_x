"""
Venice AI API integration — analysis + tweet crafting.
Jev routes each mention to a lane (router.py): DeepSeek V4.1 Flash 1017
Uncensored for unbiased answers, Kimi K3 for complex ones. The analysis model
can call X tools (x_tools.py) through Venice function calling.
Includes anti-scam URL screening, output scanning, and censorship detection.
"""
import base64
import re
import logging
import requests
from typing import Optional, List
from config import Config
from router import COMPLEX, route
from safety import (
    screen_urls,
    build_url_safety_context,
    scan_output,
    get_scam_warning_reply,
    get_injection_warning_reply,
    screen_input_for_injection,
    is_censored,
)
from venice_knowledge import relevant_faqs, get_models, summarize_models

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────
_HEADERS = {
    "Authorization": f"Bearer {Config.VENICE_API_KEY}",
    "Content-Type": "application/json",
}
_REF_RE = re.compile(r"\[REF\].*?\[/REF\]", re.DOTALL)
_FINAL_RE = re.compile(r"\[FINAL_REPLY\](.*?)\[/FINAL_REPLY\]", re.DOTALL | re.IGNORECASE)
_NOTES_RE = re.compile(r"\[NOTES\](.*?)\[/NOTES\]", re.DOTALL | re.IGNORECASE)

_BANNED = ("Hey there!", "Hi!", "Hello!", "Stay safe", "Be careful", "Be mindful")

# Keywords that indicate the query needs fresh web data
_FRESH_DATA_KEYWORDS = (
    "price", "cost", "worth", "value", "market", "tvl", "apr", "apy",
    "latest", "current", "now", "today", "recent", "new", "update",
    "news", "announce", "launch", "release", "just", "breaking",
    "vvv", "diem", "staking", "stake", "token", "crypto", "bitcoin", "btc", "eth",
    "model", "models", "venice", "feature", "api",
    "who won", "who is winning", "score", "result", "election",
    "weather", "stock", "stocks",
)


def _http_error_hint(status_code: int) -> Optional[str]:
    """Human-readable hint for a Venice HTTP error status, or None if unknown.

    Turns a bare status code into an actionable cause so logs distinguish a
    billing/auth/renamed-model problem from a transient outage.
    """
    return {
        401: "authentication failed — check VENICE_API_KEY",
        402: "payment required — the Venice account has no credits / a billing issue",
        404: "model not found — it may have been renamed or retired; check config.py",
        429: "rate limited by Venice — backing off",
    }.get(status_code)


def _strip_refs(text: str) -> str:
    return _REF_RE.sub("", text).strip()


# DeepSeek sometimes writes a tool call as text (DSML markup) instead of a
# structured tool_calls entry, and reasoning can leak into content ahead of a
# closing </think>. Neither is a reply.
_TOOL_MARKUP_RE = re.compile(
    r"<\s*[｜|]\s*DSML\s*[｜|]|<\s*/?\s*(?:tool_call|function_calls?|invoke)\b", re.IGNORECASE
)


def _clean_content(text: str) -> str:
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return _strip_refs(text.strip())


def _needs_fresh_data(query: str) -> bool:
    """Check if the query likely needs live web data."""
    q_lower = query.lower()
    return any(kw in q_lower for kw in _FRESH_DATA_KEYWORDS)


_VENICE_TOPIC_KEYWORDS = (
    "venice", "vvv", "svvv", "diem", "stake", "staking", "tokenomics",
    "token", "mint", "inference", "pro plan", "subscription", "api credit",
)
_MODEL_KEYWORDS = (
    "model", "models", "context window", "context length", "vision",
    "reasoning", "cheapest", "capabilit", "which model", "kimi", "glm",
    "qwen", "deepseek", "uncensored", "multimodal",
)


def _is_venice_topic(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in _VENICE_TOPIC_KEYWORDS)


def _is_model_question(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in _MODEL_KEYWORDS)


def _supports(model: str, capability: str, default: bool) -> bool:
    """Capability flag from the Venice model catalog (6h cache, snapshot fallback)."""
    for m in get_models():
        if m.get("id") == model:
            value = ((m.get("model_spec") or {}).get("capabilities") or {}).get(capability)
            return default if value is None else bool(value)
    return default


def _lane_chain(lane: str) -> List[tuple]:
    """(model, reasoning effort) in the order a lane tries them."""
    unbiased = [
        (Config.MODEL_UNBIASED, Config.REASONING_EFFORT_UNBIASED),
        (Config.MODEL_FALLBACK, Config.REASONING_EFFORT_UNBIASED),
        (Config.MODEL_LAST_RESORT, None),
    ]
    if lane == COMPLEX:
        return [(Config.MODEL_COMPLEX, Config.REASONING_EFFORT_COMPLEX)] + unbiased
    return unbiased


def _venice_params(urls: Optional[List[str]] = None, force_search: bool = False, web: bool = True) -> dict:
    """
    Standard Venice parameters with web search + scraping.

    force_search=True will always enable web search (for time-sensitive queries).
    Otherwise uses 'auto' to let Venice decide. web=False turns search and
    scraping off (the crafting rewrite doesn't need them).

    Note: Venice API expects string values 'on'/'off'/'auto' for enable_web_search.
    """
    params = {
        "enable_web_search": ("on" if force_search else "auto") if web else "off",
        "enable_web_scraping": web,
        "enable_web_citations": False,
        "include_venice_system_prompt": False,
    }
    if urls and web:
        params["web_search_urls"] = urls
    return params


def _chat(
    model: str,
    messages: list,
    *,
    urls: Optional[List[str]] = None,
    temperature: float = 0.7,
    force_search: bool = False,
    web: bool = True,
    reasoning_effort: Optional[str] = None,
    tools: Optional[list] = None,
    tool_choice: Optional[str] = None,
) -> Optional[dict]:
    """One chat completion request. Returns the assistant message dict or None."""
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": messages,
        "venice_parameters": _venice_params(urls, force_search=force_search, web=web),
    }
    if reasoning_effort and _supports(model, "supportsReasoningEffort", False):
        payload["reasoning_effort"] = reasoning_effort
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    try:
        r = requests.post(
            Config.VENICE_URL, json=payload, headers=_HEADERS,
            timeout=Config.VENICE_REQUEST_TIMEOUT_SECONDS,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        hint = _http_error_hint(status) if status else None
        if hint:
            # Auth/billing/renamed-model errors are config problems, not transient —
            # log them loudly so they don't hide behind the fallback cascade.
            logger.error(f"Venice API error ({model}): HTTP {status} — {hint}")
        else:
            logger.error(f"Venice API error ({model}): {e}")
        return None
    except Exception as e:
        logger.error(f"Venice API error ({model}): {e}")
        return None


def _call_venice(
    model: str,
    system: str,
    user_content,
    urls: Optional[List[str]] = None,
    temperature: float = 0.7,
    force_search: bool = False,
    *,
    reasoning_effort: Optional[str] = None,
    toolbox=None,
    web: bool = True,
) -> Optional[str]:
    """Run a completion, executing the model's X tool calls when a toolbox is given.

    Returns the final reply text (refs stripped) or None. The last allowed turn
    sets tool_choice "none" so the model has to answer with what it gathered.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    use_tools = (
        toolbox is not None
        and Config.X_TOOLS_ENABLED
        and _supports(model, "supportsFunctionCalling", True)
    )
    rounds = Config.X_TOOL_MAX_ROUNDS if use_tools else 0

    turn = retried = 0
    while turn <= rounds:
        last = turn == rounds
        msg = _chat(
            model, messages, urls=urls, temperature=temperature,
            force_search=force_search, web=web, reasoning_effort=reasoning_effort,
            tools=toolbox.schemas() if use_tools else None,
            tool_choice="none" if use_tools and last else None,
        )
        if msg is None:
            return None
        calls = msg.get("tool_calls") or []
        if not calls or last:
            text = _clean_content(msg.get("content") or "")
            if _TOOL_MARKUP_RE.search(text):
                logger.warning(f"{model} wrote a tool call as text instead of calling it")
                if retried:
                    return None
                retried = 1
                continue
            return text or None
        turn += 1

        assistant = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
        if msg.get("reasoning_content"):
            assistant["reasoning_content"] = msg["reasoning_content"]
        messages.append(assistant)
        for call in calls:
            fn = call.get("function") or {}
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": toolbox.run(fn.get("name"), fn.get("arguments")),
            })
        images = toolbox.take_images()
        if images:
            messages.append({
                "role": "user",
                "content": [{"type": "text", "text": "Images you asked to view: "
                             + "; ".join(img.label for img in images)}]
                + [{"type": "image_url", "image_url": {"url": img.data_uri()}} for img in images],
            })
    return None


# ── Public API ───────────────────────────────────────────────────

def analyse(
    query: str,
    *,
    context: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    image_url: Optional[str] = None,
    images: Optional[list] = None,
    urls: Optional[List[str]] = None,
    toolbox=None,
) -> str:
    """
    Step 1: Analyse the user's query with full context.

    `context` is the rendered X thread (x_context.ThreadContext.render) and
    `images` its XImage list; `image_bytes` / `image_url` still take a single
    image. `toolbox` (x_tools.XToolbox) lets the model call X tools.

    Model cascade (router.route picks the lane):
      complex lane:  Kimi K3 → then the unbiased lane
      unbiased lane: DeepSeek 1017 Uncensored → DeepSeek V4.1 Flash Uncensored
                     → Venice Uncensored 1.2
    An error or a refusal moves to the next model.

    SAFETY:
    - INPUT PRE-SCREENING: Token creation injection attempts are blocked immediately
    - URLs are pre-screened. Suspicious/blocked URLs are NOT scraped —
      instead the AI is told they're unverified/scam so it can warn the user.
    """
    char_limit = Config.char_limit()
    target = Config.reply_target()
    images = list(images or [])
    has_image = bool(images or image_bytes or image_url)

    # ── TOKEN CREATION INJECTION SCREENING ──
    # Check BOTH query and context for injection attempts
    full_text = f"{query} {context or ''}"
    is_safe, reason, signals = screen_input_for_injection(full_text)
    if not is_safe:
        logger.warning(f"🚫 Input blocked (injection): {reason}")
        return get_injection_warning_reply(signals)

    # ── URL SAFETY SCREENING ──
    raw_urls = urls or []
    safe_urls, suspicious_urls, blocked_urls = screen_urls(raw_urls)

    if blocked_urls:
        logger.warning(f"🚫 Blocked scam URL(s) — returning scam warning: {blocked_urls}")
        return get_scam_warning_reply(suspicious_urls, blocked_urls)

    # ── Build user message ──
    if context:
        msg = f'{context}\n\nTHE QUESTION (from the post that tagged you): "{query}"'
    else:
        msg = query

    url_context = build_url_safety_context(safe_urls, suspicious_urls, blocked_urls)
    if url_context:
        msg += f"\n\n{url_context}"

    # ── Authoritative Venice knowledge injection ──
    full_topic_text = f"{query} {context or ''}"
    knowledge_parts = []
    if _is_venice_topic(full_topic_text):
        faqs = relevant_faqs(query, context)
        if faqs:
            knowledge_parts.append("FAQ:\n" + "\n\n".join(faqs))
    if _is_model_question(full_topic_text):
        models = get_models()
        if models:
            knowledge_parts.append("CURRENT VENICE MODELS:\n" + summarize_models(models))
    if knowledge_parts:
        msg += (
            "\n\nAUTHORITATIVE VENICE FACTS (from venice.ai/faqs and the Venice "
            "models API — trust these over your own knowledge):\n"
            + "\n\n".join(knowledge_parts)
        )

    if target < char_limit:
        length_rule = (
            f"LENGTH: keep the text inside [FINAL_REPLY] under {target} characters. "
            f"Write economically and don't mention the length."
        )
    else:
        safe_limit = max(1, char_limit - 15)
        length_rule = (
            f"HARD LENGTH LIMIT: the text inside [FINAL_REPLY] MUST be {char_limit} characters "
            f"or fewer. Count the characters before finishing. Aim for {safe_limit} or fewer for "
            f"a safety margin. If your draft runs long, tighten and cut words until it fits — "
            f"never exceed {char_limit}. Going over forces a wasteful rewrite, so fit it the first time."
        )
    msg += (
        f"\n\nOUTPUT (strict):\n"
        f"[FINAL_REPLY]\n"
        f"<your tweet-ready reply, plain text, no greetings/hashtags/markdown>\n"
        f"[/FINAL_REPLY]\n\n"
        f"[NOTES]\n(optional) key facts/sources used\n[/NOTES]\n\n"
        f"{length_rule}"
    )

    # ── Build multimodal content if images are present ──
    if has_image:
        parts = [{"type": "text", "text": msg}]
        parts += [{"type": "image_url", "image_url": {"url": img.data_uri()}} for img in images]
        if image_url:
            parts.append({"type": "image_url", "image_url": {"url": image_url}})
        elif image_bytes:
            b64 = base64.b64encode(image_bytes).decode()
            parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        content = parts
    else:
        content = msg

    # ── Determine if we should force web search ──
    # Queries about prices, current events, Venice updates, etc. need fresh data
    force_search = _needs_fresh_data(full_text)
    if force_search:
        logger.info("🔍 Query needs fresh data — forcing web search")

    # ── MODEL CASCADE ──
    r = route(query, context, has_image)
    if r.decline:
        logger.warning(f"🔒 Declining a request for someone's private details: {query[:80]}")
        return f"[FINAL_REPLY]{Config.DECLINE_PRIVATE_INFO}[/FINAL_REPLY]"
    lane = r.lane
    result = censored = None
    for model, effort in _lane_chain(lane):
        out = _call_venice(
            model, Config.ANALYST_PROMPT, content,
            urls=safe_urls or None, force_search=force_search,
            reasoning_effort=effort, toolbox=toolbox,
        )
        if not out:
            logger.warning(f"{model} returned nothing — trying the next model")
            continue
        if is_censored(out):
            logger.info(f"🔓 {model} refused — trying the next model")
            censored = out
            continue
        logger.info(f"Answered with {model} ({lane} lane)")
        result = out
        break

    if not result and censored:
        logger.warning("🔓 Every model refused — using the last response as-is")
        result = censored

    return result or Config.ERROR_MESSAGE


def craft_tweet(
    analysis: str,
    *,
    use_vision: bool = False,  # kept for API compat; not used in model selection
    context_urls: Optional[List[str]] = None,
) -> str:
    """
    Step 2: Shape the analysis into a final tweet.
    If the analysis already contains a valid [FINAL_REPLY], use it directly.
    Otherwise, ask the crafter model to rewrite (DeepSeek, low reasoning, no web).

    SAFETY: Final output is scanned for scam endorsement AND censorship.
    """
    char_limit = Config.char_limit()
    target = Config.reply_target()

    # Try to extract a ready-made reply
    m = _FINAL_RE.search(analysis)
    if m:
        candidate = _strip_refs(m.group(1).strip())
        if len(candidate) <= char_limit and not any(b in candidate for b in _BANNED):
            if not is_censored(candidate):
                is_safe, reason = scan_output(candidate, context_urls)
                if not is_safe:
                    logger.warning(f"🚨 Candidate reply blocked: {reason}")
                    return get_scam_warning_reply()
                return candidate
            # Candidate was censored — fall through to uncensored rewrite
            logger.info("🔓 FINAL_REPLY was censored — rewriting with uncensored model")

    # Notes for context
    notes_m = _NOTES_RE.search(analysis)
    notes = notes_m.group(1).strip() if notes_m else ""

    source_text = m.group(1).strip() if m else analysis
    prompt = (
        f"Rewrite into a tweet (≤{target} chars). "
        f"Keep all facts. Plain text only.\n\n"
        f"SOURCE:\n{source_text}\n\n"
        f"NOTES:\n{notes}" if notes else
        f"Rewrite into a tweet (≤{target} chars). "
        f"Keep all facts. Plain text only.\n\n"
        f"SOURCE:\n{source_text}"
    )
    craft = dict(web=False, reasoning_effort=Config.REASONING_EFFORT_CRAFTER)

    result = _call_venice(Config.MODEL_UNBIASED, Config.CRAFTER_PROMPT, prompt, temperature=0.6, **craft)

    if is_censored(result):
        logger.info("🔓 Crafter censored — retrying with the fallback model")
        result = _call_venice(Config.MODEL_FALLBACK, Config.CRAFTER_PROMPT, prompt, temperature=0.6, **craft)

    if not result or len(result) > char_limit or any(b in result for b in _BANNED):
        strict = (
            f"CRITICAL: must be ≤{min(target, char_limit)} chars. No greetings, no hashtags. "
            f"Rewrite this:\n{source_text}"
        )
        result = _call_venice(Config.MODEL_UNBIASED, Config.CRAFTER_PROMPT, strict, temperature=0.5, **craft)

    if not result:
        result = _call_venice(Config.MODEL_LAST_RESORT, Config.CRAFTER_PROMPT, prompt, temperature=0.5, web=False)

    if not result:
        return Config.ERROR_MESSAGE

    # ── OUTPUT SAFETY SCAN ──
    is_safe, reason = scan_output(result, context_urls)
    if not is_safe:
        logger.warning(f"🚨 Final reply blocked: {reason}")
        # Use appropriate warning based on what was detected
        if any(kw in reason.lower() for kw in ("token", "fee", "wallet", "ticker")):
            return get_injection_warning_reply()
        return get_scam_warning_reply()

    return result
