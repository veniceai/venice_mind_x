"""Append-only JSONL log of every mention the bot handles: the route, which
models wrote the reply, what it cost, what the gate said, and whether it posted."""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)


def log_reply(record: dict, path: Optional[str] = None) -> None:
    """Append one record. Never raises: a logging failure must not stop the bot."""
    path = path or Config.REPLY_LOG_FILE
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Could not write reply log: {e}")
