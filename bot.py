import asyncio
import io
import logging
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from urllib.parse import quote

import aiohttp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import tg_api

load_dotenv()
BOT_TOKEN = os.getenv("FLIBUSTA_BOT_TOKEN", "")

FLIBUSTA_BASE = "https://flibusta.is"
FLIBUSTA_SEARCH = f"{FLIBUSTA_BASE}/opds/search"

FORMATS = {"fb2": "FB2", "epub": "EPUB", "mobi": "MOBI", "txt": "TXT", "html": "HTML", "rtf": "RTF", "pdf": "PDF"}
_FORMATS_DL = {"fb2", "epub", "mobi", "txt", "html", "rtf"}
FORMAT_EMOJI = {"fb2": "\U0001f4d8", "epub": "\U0001f4d7", "mobi": "\U0001f4d9", "txt": "\U0001f4c4", "html": "\U0001f310", "rtf": "\U0001f4dd", "pdf": "\U0001f4d5"}
_DEFAULT_EMOJI = "\U0001f4c4"
BOOKS_PER_PAGE = 5
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300)
SEARCH_TIMEOUT = aiohttp.ClientTimeout(total=15)
HEADERS = {"User-Agent": "FlibustaBot/1.0"}

RE_BOOK_ID = re.compile(r"/b/(\d+)/(\w+)$")
RE_ALT_ID = re.compile(r"/b/(\d+)$")
RE_ID_NUM = re.compile(r"(\d+)")
RE_HTML_TAGS = re.compile(r"<[^>]+>")
RE_CONTENT_FMT = re.compile(r"Формат:\s*(\w+)", re.IGNORECASE)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
user_cache: dict[int, list[dict]] = {}
user_last_query: dict[int, str] = {}
_http_session: aiohttp.ClientSession | None = None

_RATE_WINDOW = 60
_RATE_LIMITS = {"search": 10, "download": 30, "page": 30}
_rate_hits: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
_draft_counter: dict[int, int] = defaultdict(int)
_user_cache_time: dict[int, float] = {}
_CACHE_TTL = 3600


def _check_rate(user_id: int, action: str) -> bool:
    now = time.monotonic()
    hits = _rate_hits[user_id][action]
    cutoff = now - _RATE_WINDOW
    hits[:] = [t for t in hits if t > cutoff]
    limit = _RATE_LIMITS.get(action, 10)
    if len(hits) >= limit:
        return False
    hits.append(now)
    return True


def _next_draft_id(user_id: int) -> int:
    _draft_counter[user_id] += 1
    return _draft_counter[user_id]


def _cache_cleanup():
    now = time.time()
    stale = [uid for uid, t in _user_cache_time.items() if now - t > _CACHE_TTL]
    for uid in stale:
        user_cache.pop(uid, None)
        _user_cache_time.pop(uid, None)


async def _safe_answer(callback: CallbackQuery, text: str = "", show_alert: bool = False):
    try:
        await callback.answer(text, show_alert=show_alert)
    except Exception:
        pass


async def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(headers=HEADERS)
    return _http_session


async def close_session():
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None


async def search_books(query: str) -> list[dict]:
    url = f"{FLIBUSTA_SEARCH}?searchType=books&searchTerm={quote(query)}"
    session = await get_session()
    try:
        async with session.get(url, timeout=SEARCH_TIMEOUT) as resp:
            if resp.status != 200:
                logger.error("Search failed: %d", resp.status)
                return []
            data = await resp.text()
    except aiohttp.ClientError as e:
        logger.error("Search error: %s", e)
        return []

    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        logger.error("Failed to parse OPDS XML")
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    books = []

    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        if title_el is None or not title_el.text:
            continue

        authors = [
            a.findtext("atom:name", default="", namespaces=ns)
            for a in entry.findall("atom:author", ns)
        ]
        authors = [a.strip() for a in authors if a.strip()]

        genre_el = entry.find("atom:category", ns)
        genre = genre_el.get("term", "") if genre_el is not None else ""

        book_id = ""
        formats = []

        for link in entry.findall("atom:link", ns):
            href = link.get("href", "")
            rel = link.get("rel", "")
            if rel == "http://opds-spec.org/acquisition/open-access":
                m = RE_BOOK_ID.search(href)
                if m:
                    fmt = m.group(2)
                    if not book_id:
                        book_id = m.group(1)
                    if fmt in FORMATS:
                        formats.append(fmt)
                elif "/download" in href and not book_id:
                    m = re.search(r"/b/(\d+)", href)
                    if m:
                        book_id = m.group(1)
            elif rel == "alternate" and not book_id:
                m = RE_ALT_ID.search(href)
                if m:
                    book_id = m.group(1)

        if not book_id:
            id_text = entry.findtext("atom:id", default="", namespaces=ns)
            m = RE_ID_NUM.search(id_text)
            if m:
                book_id = m.group(1)

        if not book_id:
            continue

        content = entry.findtext("atom:content", default="", namespaces=ns)
        description = ""
        size_info = ""
        if content:
            raw = content.replace("<br/>", "\n").replace("<br>", "\n")
            raw = RE_HTML_TAGS.sub("", raw).strip()
            for line in raw.split("\n"):
                line = line.strip()
                if "Размер:" in line:
                    size_info = line.split("Размер:")[-1].strip()
                elif "Скачиваний:" in line:
                    size_info += f" | {line.split('Скачиваний:')[-1].strip()} загрузок"
            description = " ".join(l.strip() for l in raw.split("\n") if l.strip())[:300]

            if not formats:
                m = RE_CONTENT_FMT.search(content)
                if m:
                    fmt = m.group(1).lower()
                    formats.append(fmt if fmt in FORMATS else "download")

        if not formats:
            formats = ["download"]

        books.append({
            "id": book_id,
            "title": title_el.text.strip(),
            "author": ", ".join(authors) if authors else "Неизвестный автор",
            "genre": genre,
            "formats": sorted(formats),
            "description": description,
            "size_info": size_info,
        })

    return books


async def download_book(book_id: str, fmt: str) -> tuple[bytes | None, str]:
    path = fmt if fmt in _FORMATS_DL else "download"
    url = f"{FLIBUSTA_BASE}/b/{book_id}/{path}"
    session = await get_session()
    try:
        async with session.get(url, timeout=DOWNLOAD_TIMEOUT) as resp:
            logger.info("Download %s -> %s status=%d ct=%s size=%s",
                        url, resp.url, resp.status,
                        resp.headers.get("Content-Type", "?"),
                        resp.headers.get("Content-Length", "?"))
            if resp.status != 200:
                logger.error("Download failed: %d", resp.status)
                return None, ""
            content = await resp.read()
            cd = resp.headers.get("Content-Disposition", "")
    except aiohttp.ClientError as e:
        logger.error("Download error: %s", e)
        return None, ""

    logger.info("Downloaded %d bytes from %s", len(content), url)

    if len(content) < 100:
        return None, ""

    filename = ""
    if "filename=" in cd:
        filename = cd.split("filename=")[-1].strip('" ')

    if content[:2] == b"PK" and zipfile.is_zipfile(io.BytesIO(content)) and path == "download":
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = [n for n in zf.namelist() if not n.startswith("META-INF") and n != "mimetype"]
            if names:
                content = zf.read(names[0])
                filename = names[0]
                logger.info("Extracted from ZIP: %s (%d bytes)", filename, len(content))

    if not filename:
        filename = f"book_{book_id}.{fmt}"

    return content, filename


def _fmt_formats(formats: list[str]) -> str:
    visible = [f for f in formats if f != "download"]
    if not visible:
        return "\U0001f4e5 \u0421\u043a\u0430\u0447\u0430\u0442\u044c"
    return " \u00b7 ".join(f"{FORMAT_EMOJI.get(f, _DEFAULT_EMOJI)}`{f.upper()}`" for f in visible)


def format_search_rich(books: list[dict], page: int, query: str = "") -> str:
    total = len(books)
    start = page * BOOKS_PER_PAGE
    end = min(start + BOOKS_PER_PAGE, total)
    pages_total = (total + BOOKS_PER_PAGE - 1) // BOOKS_PER_PAGE

    if query:
        lines = [
            f"# \U0001f4da \u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u044b \u043f\u043e\u0438\u0441\u043a\u0430",
            f"\U0001f50d \u0417\u0430\u043f\u0440\u043e\u0441: `{query}` \u2014 **{total} \u043d\u0430\u0439\u0434\u0435\u043d\u043e**",
        ]
    else:
        lines = [
            f"# \U0001f4da \u0421\u0442\u0440. {page + 1} \u0438\u0437 {pages_total}",
        ]

    for i, book in enumerate(books[start:end], start=start + 1):
        lines.append("---")
        lines.append(f"**{i}. {book['title']}**")
        lines.append(f"\u270d\ufe0f {book['author']}")
        lines.append(f"{_fmt_formats(book['formats'])}")

    lines.append("---")
    if total > end:
        lines.append(f"*\u25bc \u0421\u0442\u0440. {page + 2}. \u041b\u0438\u0441\u0442\u0430\u0439\u0442\u0435 \u043a\u043d\u043e\u043f\u043a\u0430\u043c\u0438 \u2193*")

    return "\n".join(lines)


def format_book_detail(book: dict) -> str:
    lines = [
        f"# \U0001f4d6 {book['title']}",
        "---",
        f"\u270d\ufe0f **\u0410\u0432\u0442\u043e\u0440:** {book['author']}",
    ]
    if book["genre"]:
        lines.append(f"\U0001f3f7\ufe0f **\u0416\u0430\u043d\u0440:** {book['genre']}")
    if book["size_info"]:
        lines.append(f"\U0001f4ca {book['size_info']}")
    if book["description"]:
        desc = book["description"][:260] + "..." if len(book["description"]) > 260 else book["description"]
        lines.append(f"\n> {desc}")
    visible_fmts = [f for f in book["formats"] if f != "download"]
    if visible_fmts:
        lines.append(f"\n---\n**\U0001f4e5 \u0414\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0435 \u0444\u043e\u0440\u043c\u0430\u0442\u044b:**")
        for fmt in visible_fmts:
            lines.append(f"- {FORMAT_EMOJI.get(fmt, _DEFAULT_EMOJI)} **{FORMATS.get(fmt, fmt.upper())}**")
    else:
        lines.append(f"\n---\n**\U0001f4e5 \u0421\u043a\u0430\u0447\u0430\u0442\u044c \u043a\u043d\u0438\u043a\u0443**")
    lines.append("\n---\n*\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0444\u043e\u0440\u043c\u0430\u0442:*")
    return "\n".join(lines)


def format_book_detail(book: dict) -> str:
    lines = [
        f"# \U0001f4d6 {book['title']}",
        "---",
        f"> \u270d\ufe0f **\u0410\u0432\u0442\u043e\u0440:** {book['author']}",
    ]
    if book["genre"]:
        lines.append(f"> \U0001f3f7\ufe0f **\u0416\u0430\u043d\u0440:** {book['genre']}")
    if book["size_info"]:
        lines.append(f"> \U0001f4ca {book['size_info']}")
    if book["description"]:
        desc = book["description"][:260] + "..." if len(book["description"]) > 260 else book["description"]
        lines.append(f"\n---\n> {desc}")
    lines.append("\n---")
    visible_fmts = [f for f in book["formats"] if f != "download"]
    if visible_fmts:
        lines.append(f"**\U0001f4e5 \u0414\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0435 \u0444\u043e\u0440\u043c\u0430\u0442\u044b:**\n")
        for fmt in visible_fmts:
            lines.append(f"- {FORMAT_EMOJI.get(fmt, _DEFAULT_EMOJI)} **{FORMATS.get(fmt, fmt.upper())}**")
    else:
        lines.append(f"**\U0001f4e5 \u0421\u043a\u0430\u0447\u0430\u0442\u044c \u043a\u043d\u0438\u0433\u0443**\n")
    lines.append("\n---\n*\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0444\u043e\u0440\u043c\u0430\u0442:*")
    return "\n".join(lines)


def _build_keyboard(books: list[dict], page: int) -> InlineKeyboardMarkup:
    total = len(books)
    start = page * BOOKS_PER_PAGE
    end = min(start + BOOKS_PER_PAGE, total)

    buttons = [
        [InlineKeyboardButton(
            text=f"{i}. {books[i - 1]['title'][:45]}",
            callback_data=f"book_{books[i - 1]['id']}_{page}",
        )]
        for i in range(start + 1, end + 1)
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="\u25c0\ufe0f \u041d\u0430\u0437\u0430\u0434", callback_data=f"page_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"\U0001f4c4 {start + 1}\u2013{end} \u0438\u0437 {total}", callback_data="noop"))
    if end < total:
        nav.append(InlineKeyboardButton(text="\u0412\u043f\u0435\u0440\u0435\u0434 \u25b6\ufe0f", callback_data=f"page_{page + 1}"))
    buttons.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_formats_keyboard(book_id: str, formats: list[str], page: int = 0) -> InlineKeyboardMarkup:
    visible = [f for f in formats if f != "download"]
    buttons = []
    if visible:
        row = []
        for fmt in visible:
            row.append(InlineKeyboardButton(
                text=f"{FORMAT_EMOJI.get(fmt, _DEFAULT_EMOJI)} {FORMATS.get(fmt, fmt.upper())}",
                callback_data=f"dl_{book_id}_{fmt}",
            ))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
    else:
        buttons.append([InlineKeyboardButton(
            text="\U0001f4e5 \u0421\u043a\u0430\u0447\u0430\u0442\u044c",
            callback_data=f"dl_{book_id}_download",
        )])
    buttons.append([InlineKeyboardButton(text="\U0001f519 \u041a \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u0430\u043c \u043f\u043e\u0438\u0441\u043a\u0430", callback_data=f"back_{page}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _kb_to_dict(kb: InlineKeyboardMarkup) -> dict:
    return {
        "inline_keyboard": [
            [{"text": b.text, "callback_data": b.callback_data} for b in row]
            for row in kb.inline_keyboard
        ]
    }


@router.message(CommandStart())
async def cmd_start(message: Message):
    md = (
        "# \U0001f4da \u0424\u043b\u0438\u0431\u0443\u0441\u0442\u0430 \u2014 \u041a\u043d\u0438\u0436\u043d\u044b\u0439 \u0431\u043e\u0442\n"
        "---\n"
        "\U0001f50d \u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 **\u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u043a\u043d\u0438\u0433\u0438** \u0438\u043b\u0438 **\u0438\u043c\u044f \u0430\u0432\u0442\u043e\u0440\u0430**\n"
        "\U0001f4e5 \u0421\u043a\u0430\u0447\u0438\u0432\u0430\u043d\u0438\u0435 \u0432 \u0444\u043e\u0440\u043c\u0430\u0442\u0430\u0445: FB2, EPUB, MOBI, TXT, HTML, RTF, PDF"
    )
    await tg_api.send_rich_message(BOT_TOKEN, message.chat.id, markdown=md)


@router.message(F.text)
async def handle_search(message: Message):
    uid = message.from_user.id
    query = message.text.strip()
    if len(query) < 2:
        await message.answer("\u274c \u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043c\u0438\u043d\u0438\u043c\u0443\u043c 2 \u0441\u0438\u043c\u0432\u043e\u043b\u0430 \u0434\u043b\u044f \u043f\u043e\u0438\u0441\u043a\u0430.")
        return

    if not _check_rate(uid, "search"):
        await message.answer("\u23f0 \u0421\u043b\u0438\u0448\u043a\u043e\u043c \u0447\u0430\u0441\u0442\u043e! \u041f\u043e\u0434\u043e\u0436\u0434\u0438\u0442\u0435 \u043c\u0438\u043d\u0443\u0442\u0443.")
        return

    draft_id = _next_draft_id(uid)
    await tg_api.send_rich_draft(BOT_TOKEN, message.chat.id, draft_id, markdown="# \U0001f50d \u0418\u0449\u0443...")

    books = await search_books(query)

    if not books:
        kb = _kb_to_dict(InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f504 \u041f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u044c \u043f\u043e\u0438\u0441\u043a", callback_data=f"repeat_{uid}")]
        ]))
        await tg_api.send_rich_message(
            BOT_TOKEN, message.chat.id,
            markdown=f"# \U0001f614 \u041d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e\n\n\u0417\u0430\u043f\u0440\u043e\u0441: `{query}`\n\n*\u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0434\u0440\u0443\u0433\u043e\u0439 \u0437\u0430\u043f\u0440\u043e\u0441*",
            reply_markup=kb,
        )
        return

    user_cache[uid] = books
    user_last_query[uid] = query
    _user_cache_time[uid] = time.time()

    md = format_search_rich(books, 0, query)
    kb = _build_keyboard(books, 0)
    await tg_api.send_rich_message(
        BOT_TOKEN, message.chat.id, markdown=md, reply_markup=_kb_to_dict(kb),
    )


@router.callback_query(F.data.startswith("page_"))
async def handle_page(callback: CallbackQuery):
    uid = callback.from_user.id
    if not _check_rate(uid, "page"):
        await _safe_answer(callback, "\u23f0 \u0421\u043b\u0438\u0448\u043a\u043e\u043c \u0447\u0430\u0441\u0442\u043e!", True)
        return
    page = int(callback.data.split("_")[1])
    books = user_cache.get(uid, [])
    if not books:
        await _safe_answer(callback, "\u26a0\ufe0f \u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u044b \u0443\u0441\u0442\u0430\u0440\u0435\u043b\u0438.", True)
        return
    md = format_search_rich(books, page)
    kb = _build_keyboard(books, page)
    await tg_api.edit_rich_message(
        BOT_TOKEN, callback.message.chat.id, callback.message.message_id,
        markdown=md, reply_markup=_kb_to_dict(kb),
    )
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("book_"))
async def handle_book(callback: CallbackQuery):
    uid = callback.from_user.id
    parts = callback.data.split("_")
    book_id = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0
    books = user_cache.get(uid, [])
    book = next((b for b in books if b["id"] == book_id), None)

    if not book:
        await _safe_answer(callback, "\u26a0\ufe0f \u041a\u043d\u0438\u0433\u0430 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430.", True)
        return

    md = format_book_detail(book)
    kb = build_formats_keyboard(book_id, book["formats"], page)
    await tg_api.edit_rich_message(
        BOT_TOKEN, callback.message.chat.id, callback.message.message_id,
        markdown=md, reply_markup=_kb_to_dict(kb),
    )
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("dl_"))
async def handle_download(callback: CallbackQuery):
    uid = callback.from_user.id
    if not _check_rate(uid, "download"):
        await _safe_answer(callback, "\u23f0 \u0421\u043b\u0438\u0448\u043a\u043e\u043c \u0447\u0430\u0441\u0442\u043e! \u041f\u043e\u0434\u043e\u0436\u0434\u0438\u0442\u0435.", True)
        return

    _, book_id, fmt = callback.data.split("_", 2)
    books = user_cache.get(uid, [])
    book = next((b for b in books if b["id"] == book_id), None)

    if not book:
        await _safe_answer(callback, "\u26a0\ufe0f \u041a\u043d\u0438\u043a\u0430 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430.", True)
        return

    fmt_name = FORMATS.get(fmt, fmt.upper())
    emoji = FORMAT_EMOJI.get(fmt, "\U0001f4c4")

    await _safe_answer(callback, f"\U0001f4e5 \u0421\u043a\u0430\u0447\u0438\u0432\u0430\u044e {fmt_name}...")

    data, filename = await download_book(book_id, fmt)
    if data is None:
        await callback.message.answer(f"\u274c \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0441\u043a\u0430\u0447\u0430\u0442\u044c **{book['title']}** (`{fmt_name}`)", parse_mode="Markdown")
        return

    chat_id = callback.message.chat.id
    try:
        await callback.message.delete()
    except Exception:
        pass

    size_mb = len(data) / (1024 * 1024)
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        doc = FSInputFile(tmp_path, filename=filename)
        repeat_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f504 \u041f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u044c \u043f\u043e\u0438\u0441\u043a", callback_data=f"repeat_{uid}")]
        ])
        logger.info("Sending document: chat=%s file=%s size=%d", chat_id, filename, os.path.getsize(tmp_path))
        result = await callback.bot.send_document(
            chat_id=chat_id,
            document=doc,
            caption=f"{emoji} **{book['title']}**\n\U0001f4d6 {book['author']}\n\U0001f4e6 {fmt_name} | {size_mb:.1f} MB",
            parse_mode="Markdown",
            reply_markup=_kb_to_dict(repeat_kb),
        )
        logger.info("Document sent: file_id=%s size=%s", result.document.file_id, result.document.file_size)
    finally:
        os.unlink(tmp_path)


@router.callback_query(F.data.startswith("back_"))
async def handle_back_search(callback: CallbackQuery):
    uid = callback.from_user.id
    page = int(callback.data.split("_", 1)[1])
    books = user_cache.get(uid, [])
    if not books:
        await _safe_answer(callback, "\u26a0\ufe0f \u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u044b \u0443\u0441\u0442\u0430\u0440\u0435\u043b\u0438.", True)
        return
    md = format_search_rich(books, page)
    kb = _build_keyboard(books, page)
    await tg_api.edit_rich_message(
        BOT_TOKEN, callback.message.chat.id, callback.message.message_id,
        markdown=md, reply_markup=_kb_to_dict(kb),
    )
    await _safe_answer(callback)


@router.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery):
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("repeat_"))
async def handle_repeat(callback: CallbackQuery):
    uid = int(callback.data.split("_", 1)[1])
    query = user_last_query.get(uid, "")
    if not query:
        await _safe_answer(callback, "\u26a0\ufe0f \u041d\u0435\u0442 \u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d\u043d\u043e\u0433\u043e \u0437\u0430\u043f\u0440\u043e\u0441\u0430.", True)
        return

    if not _check_rate(uid, "search"):
        await _safe_answer(callback, "\u23f0 \u0421\u043b\u0438\u0448\u043a\u043e\u043c \u0447\u0430\u0441\u0442\u043e!", True)
        return

    await _safe_answer(callback, f"\U0001f504 \u041f\u043e\u0432\u0442\u043e\u0440\u044f\u044e: {query}")

    books = await search_books(query)
    if not books:
        await tg_api.edit_rich_message(
            BOT_TOKEN, callback.message.chat.id, callback.message.message_id,
            markdown=f"# \U0001f614 \u041d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e\n\n\u0417\u0430\u043f\u0440\u043e\u0441: `{query}`\n\n*\u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0434\u0440\u0443\u0433\u043e\u0439 \u0437\u0430\u043f\u0440\u043e\u0441*",
        )
        return

    user_cache[uid] = books
    _user_cache_time[uid] = time.time()

    md = format_search_rich(books, 0, query)
    kb = _build_keyboard(books, 0)
    await tg_api.send_rich_message(
        BOT_TOKEN, callback.message.chat.id, markdown=md, reply_markup=_kb_to_dict(kb),
    )


async def main():
    if not BOT_TOKEN:
        print("\u0423\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u0435 \u043f\u0435\u0440\u0435\u043c\u0435\u043d\u043d\u0443\u044e FLIBUSTA_BOT_TOKEN")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    print("\U0001f916 \u0411\u043e\u0442 \u0437\u0430\u043f\u0443\u0449\u0435\u043d!")
    try:
        await dp.start_polling(bot)
    finally:
        await close_session()
        await tg_api.close_session()


if __name__ == "__main__":
    asyncio.run(main())
