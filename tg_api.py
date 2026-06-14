import json
import logging

import aiohttp

logger = logging.getLogger(__name__)

TG_API = "https://api.telegram.org/bot{token}/{method}"
_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


async def _post(method: str, token: str, data: dict) -> dict | None:
    url = TG_API.format(token=token, method=method)
    session = await _get_session()
    async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        result = await resp.json()
        if not result.get("ok"):
            logger.error("%s failed: %s", method, result.get("description"))
        return result


def _rich_payload(chat_id: int, markdown: str | None = None, html: str | None = None, **extra) -> dict:
    p: dict = {"chat_id": chat_id}
    rich: dict = {}
    if markdown:
        rich["markdown"] = markdown
    elif html:
        rich["html"] = html
    p["rich_message"] = json.dumps(rich)
    for k, v in extra.items():
        if v is not None:
            p[k] = json.dumps(v) if isinstance(v, dict) else v
    return p


async def send_rich_message(token: str, chat_id: int, markdown: str | None = None, html: str | None = None, reply_markup: dict | None = None, disable_notification: bool = False) -> dict | None:
    if not markdown and not html:
        return None
    p = _rich_payload(chat_id, markdown, html, reply_markup=reply_markup)
    if disable_notification:
        p["disable_notification"] = "true"
    return await _post("sendRichMessage", token, p)


async def send_rich_draft(token: str, chat_id: int, draft_id: int, markdown: str | None = None, html: str | None = None) -> dict | None:
    if not markdown and not html:
        return None
    p = _rich_payload(chat_id, markdown, html)
    p["draft_id"] = str(draft_id)
    return await _post("sendRichMessageDraft", token, p)


async def edit_rich_message(token: str, chat_id: int, message_id: int, markdown: str | None = None, html: str | None = None, reply_markup: dict | None = None) -> dict | None:
    p = _rich_payload(chat_id, markdown, html, message_id=message_id, reply_markup=reply_markup)
    return await _post("editMessageText", token, p)
