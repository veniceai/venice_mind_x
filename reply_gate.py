"""Last check on a reply before it posts, judged by Clef.

The uncensored lane model is abliterated, so the prompt's rules may not hold on
their own. This asks the decision model whether the final reply crosses a hard
line, states something about a real person or recent event that the thread
and sources don't support, speculates about anyone's health, private life or
alleged wrongdoing, or quotes
returns for Venice's tokens. It is deliberately narrow (everything else posts
as written) and fails open: if the check itself errors, the reply posts and
the error is logged at ERROR.
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
    "unsupported": {
        "type": "noul",
        "instructions": (
            "Does `reply` state as established fact a claim about a real person's recent "
            "actions, statements, health or death, or about a recent event, that neither "
            "`thread` nor `sources` supports? A claim the reply itself labels unverified, "
            "a rumor or disputed does not count."
        ),
    },
    "speculation": {
        "type": "noul",
        "instructions": (
            "Does `reply` speculate about, or insinuate something about, any person's health, "
            "private life or alleged wrongdoing beyond what `thread` or `sources` report? This "
            "covers public figures too. Plainly reporting what a source says, with the source, "
            "does not count."
        ),
    },
    "token_returns": {
        "type": "noul",
        "instructions": (
            "Does `reply` state or project a yield, return, APR, APY, ROI or percentage "
            "gain or loss for VVV, sVVV or DIEM?"
        ),
    },
}


def crossed_line(reply: str, thread: str = "", sources: str = "") -> Optional[str]:
    """The first check `reply` fails, or None. Fails open."""
    if not Config.REPLY_GATE_ENABLED or not reply:
        return None
    state = {
        "reply": reply,
        "thread": thread[-Config.GATE_THREAD_MAX_CHARS:],
        "sources": sources[-Config.GATE_SOURCES_MAX_CHARS:],
    }
    try:
        r = requests.post(
            Config.VENICE_DECISIONS_URL,
            json={"model": Config.MODEL_ROUTER, "state": state, "questions": QUESTIONS},
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
        logger.error(f"Reply gate failed ({e}); posting without it")
        return None
    hits = [k for k, p in scores.items() if p >= Config.REPLY_GATE_THRESHOLD]
    if hits:
        logger.warning(f"⛔ Reply fails the gate ({', '.join(hits)}): {scores}")
        return hits[0]
    return None
