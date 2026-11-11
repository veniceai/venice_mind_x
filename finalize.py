"""The last steps before a reply posts: X's rules, the token-returns backstop
and the Clef gate, with one rewrite for the checks a rewrite can fix."""
import logging
from typing import Iterable, Optional, Tuple

from reply_gate import crossed_line
from safety import mentions_token_returns
from twitter_client import fit_x_rules
from venice_api import rewrite_reply

logger = logging.getLogger(__name__)


def finalize_reply(
    text: str, *, thread: str = "", sources: str = "", participants: Iterable[str] = ()
) -> Tuple[Optional[str], Optional[str]]:
    """Returns (reply to post, None) or (None, the check it failed)."""
    participants = list(participants)
    for attempt in range(2):
        text = fit_x_rules(text, participants)
        failed = "token_returns" if mentions_token_returns(text) else crossed_line(text, thread, sources)
        if not failed:
            return text, None
        rewritten = rewrite_reply(text, failed) if attempt == 0 else None
        if not rewritten:
            logger.warning(f"Not posting: the reply fails the {failed} check")
            return None, failed
        logger.info(f"Rewrote the reply after the {failed} check")
        text = rewritten
    return None, failed
