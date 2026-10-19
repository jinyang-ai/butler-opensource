"""Reach the user from any thread: `send` = new message (problems only), `event` = silent dashboard update."""

import asyncio
import logging

log = logging.getLogger(__name__)

_bot = None
_loop: asyncio.AbstractEventLoop | None = None
_chat_ids: set[int] = set()
_on_event = None  # async fn(text): refresh the dashboard silently


def bind(bot, loop: asyncio.AbstractEventLoop, chat_ids: set[int], on_event=None) -> None:
    global _bot, _loop, _chat_ids, _on_event
    _bot, _loop, _chat_ids, _on_event = bot, loop, chat_ids, on_event


def event(text: str) -> None:
    """Routine happenings (scheduled on/off, auto-off): update the dashboard in place, no new message."""
    if _on_event and _loop:
        asyncio.run_coroutine_threadsafe(_on_event(text), _loop)


def send(text: str) -> None:
    if not (_bot and _loop and _chat_ids):
        return
    for cid in _chat_ids:
        try:
            asyncio.run_coroutine_threadsafe(_bot.send_message(cid, text), _loop)
        except Exception:
            log.exception("notify failed")
