"""Last check on a reply before it posts: the hard lines, judged by Clef.

The uncensored lane model is abliterated, so the prompt's HARD LINES may not
hold on their own. This asks the decision model whether the final reply
crosses one of them. It is deliberately narrow (everything else posts as
written) and fails open: if the check itself errors, the reply posts and the
error is logged.
"""
import logging
from typing import Optional

import requests

from config import Config

logger = logging.getLogger(__name__)

QUESTIONS = {
    "minors": {
        "type": "noul",
        "instructions": "Does `reply` contain sexual content involving anyone under 18?",
    },
    "private_info": {
        "type": "noul",
        "instructions": (
            "Does `reply` disclose, offer to find, or explain how to find a specific "
            "private individual's real identity, location or contact details, such as a "
            "home address, phone number, personal email, workplace or ID number?"
        ),
    },
    "threat": {
        "type": "noul",
        "instructions": "Does `reply` threaten, or call for violence against, a real person or group?",
    },
    "mass_harm": {
        "type": "noul",
        "instructions": (
            "Does `reply` give actionable instructions for making or deploying chemical, "
            "biological, nuclear or radiological weapons, or explosives meant for mass casualties?"
        ),
    },
}


def crossed_line(reply: str) -> Optional[str]:
    """The hard line `reply` crosses, or None. Fails open."""
    if not Config.REPLY_GATE_ENABLED or not reply:
        return None
    try:
        r = requests.post(
            Config.VENICE_DECISIONS_URL,
            json={"model": Config.MODEL_ROUTER, "state": {"reply": reply}, "questions": QUESTIONS},
            headers={
                "Authorization": f"Bearer {Config.VENICE_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=Config.ROUTER_TIMEOUT_SECONDS,
        )
        r.raise_for_status()
        answers = r.json()["answers"]
        scores = {k: float(answers[k]["noul"]) for k in QUESTIONS}
    except Exception as e:
        logger.warning(f"Reply gate failed ({e}); posting without it")
        return None
    hits = [k for k, p in scores.items() if p >= Config.REPLY_GATE_THRESHOLD]
    if hits:
        logger.warning(f"⛔ Reply crosses a hard line ({', '.join(hits)}): {scores}")
        return hits[0]
    return None
