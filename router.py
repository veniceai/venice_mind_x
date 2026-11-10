"""Lane routing: one decision-model call per mention picks the model lane (plan 0002).

Clef (Cloudflare's decision model, private) sits behind Venice's POST /decisions:
it reads a state and answers typed questions with probabilities instead of
generating text. The bot asks four yes/no questions about each mention: three
pick the lane (`choose_lane`), the fourth flags requests to dox someone, which
get a one-line decline. Any failure routes to the uncensored lane.
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
    "filtered": {
        "type": "noul",
        "instructions": (
            "Is `question` about a topic that a safety-filtered AI might refuse, water "
            "down or hedge, such as drugs, sex, violence, weapons, crime, hacking, "
            "extremism, conspiracy theories, medical, legal or financial gray areas, "
            "or offensive humor?"
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
    "private_info": {
        "type": "noul",
        "instructions": (
            "Does `question` ask to find or reveal a specific person's real identity, "
            "home address, phone number, personal email, workplace or other private "
            "details (not an organization's public contact)?"
        ),
    },
}


@dataclass
class Route:
    lane: str
    charged: Optional[float] = None
    filtered: Optional[float] = None
    complex: Optional[float] = None
    private_info: Optional[float] = None
    reason: str = ""

    @property
    def decline(self) -> bool:
        """The question asks the bot to dox someone: answer with a one-line no."""
        return self.private_info is not None and self.private_info >= Config.ROUTER_PRIVATE_INFO_THRESHOLD


def choose_lane(charged: Optional[float], filtered: Optional[float], complex_: Optional[float]) -> str:
    """Map the decision model's probabilities to a lane.

    Anything charged or likely to be filtered stays on the uncensored model, so
    the safety-tuned complex model only sees questions it won't soften.
    """
    if charged is None or filtered is None or complex_ is None:
        return UNBIASED
    if charged >= Config.ROUTER_CHARGED_THRESHOLD or filtered >= Config.ROUTER_FILTERED_THRESHOLD:
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
        filtered = float(answers["filtered"]["noul"])
        complex_ = float(answers["complex"]["noul"])
        private_info = float(answers["private_info"]["noul"])
    except Exception as e:
        logger.warning(f"Router ({Config.MODEL_ROUTER}) failed ({e}); using the uncensored lane")
        return Route(UNBIASED, reason=f"router error: {e}")

    r = Route(choose_lane(charged, filtered, complex_), charged, filtered, complex_, private_info)
    logger.info(
        f"🧭 Route: {r.lane}{' (decline)' if r.decline else ''} (charged={charged:.2f}, "
        f"filtered={filtered:.2f}, complex={complex_:.2f}, private_info={private_info:.2f})"
    )
    return r
