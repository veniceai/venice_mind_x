"""Lane routing: one Jev decision per mention picks the model lane (plan 0002).

Jev is a decision model behind Venice's POST /decisions: it reads a state and
answers typed questions with probabilities instead of generating text. The bot
asks two yes/no questions about each mention and maps the answers to a lane
with `choose_lane`. Any failure routes to the unbiased lane.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import requests

from config import Config

logger = logging.getLogger(__name__)

UNBIASED = "unbiased"
COMPLEX = "complex"

QUESTIONS = {
    "charged": {
        "type": "noul",
        "instructions": (
            "Does answering `question` require a judgment on a contested political, "
            "social, cultural, religious or moral issue, on a public figure, or on "
            "whether a disputed claim in `question` or `thread` is true?"
        ),
    },
    "complex": {
        "type": "noul",
        "instructions": (
            "Does a good answer to `question` require several steps of reasoning, "
            "such as math, code, a technical or scientific explanation, analysis of "
            "a long document, chart or thread, or weighing several sources against "
            "each other?"
        ),
    },
}


@dataclass
class Route:
    lane: str
    charged: Optional[float] = None
    complex: Optional[float] = None
    reason: str = ""


def choose_lane(charged: Optional[float], complex_: Optional[float]) -> str:
    """Map Jev's probabilities to a lane. Charged questions always stay unbiased."""
    if charged is None or complex_ is None:
        return UNBIASED
    if charged >= Config.ROUTER_CHARGED_THRESHOLD:
        return UNBIASED
    if complex_ >= Config.ROUTER_COMPLEX_THRESHOLD:
        return COMPLEX
    return UNBIASED


def build_state(query: str, context: Optional[str], has_image: bool) -> dict:
    thread = context or ""
    if len(thread) > Config.ROUTER_STATE_MAX_CHARS:
        # Keep the end: the posts nearest the mention matter most.
        thread = thread[-Config.ROUTER_STATE_MAX_CHARS:]
    return {"question": query, "thread": thread, "has_image": has_image}


def route(query: str, context: Optional[str] = None, has_image: bool = False) -> Route:
    if not Config.ROUTER_ENABLED:
        return Route(UNBIASED, reason="router disabled")

    payload = {
        "model": Config.MODEL_ROUTER,
        "state": build_state(query, context, has_image),
        "questions": QUESTIONS,
    }
    try:
        r = requests.post(
            Config.VENICE_DECISIONS_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {Config.VENICE_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=Config.ROUTER_TIMEOUT_SECONDS,
        )
        r.raise_for_status()
        answers = r.json()["answers"]
        charged = float(answers["charged"]["noul"])
        complex_ = float(answers["complex"]["noul"])
    except Exception as e:
        logger.warning(f"Router ({Config.MODEL_ROUTER}) failed ({e}); using the unbiased lane")
        return Route(UNBIASED, reason=f"router error: {e}")

    lane = choose_lane(charged, complex_)
    logger.info(f"🧭 Route: {lane} (charged={charged:.2f}, complex={complex_:.2f})")
    return Route(lane, charged, complex_)
