import io
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import aiohttp

import bot as flibusta_bot
import tg_api


SAMPLE_OPDS = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Тестовая книга</title>
    <author><name>Тестовый Автор</name></author>
    <category term="Фантастика"/>
    <content type="text/html">Формат: fb2&lt;br/&gt;Размер: 500 Kb&lt;br/&gt;Скачиваний: 42</content>
    <link href="/b/12345/fb2" rel="http://opds-spec.org/acquisition/open-access"/>
    <link href="/b/12345/epub" rel="http://opds-spec.org/acquisition/open-access"/>
  </entry>
  <entry>
    <title>Вторая книга</title>
    <author><name>Автор Два</name></author>
    <author><name>Автор Три</name></author>
    <category term="Детектив"/>
    <content type="text/html">Размер: 1.2 Mb&lt;br/&gt;Скачиваний: 999</content>
    <link href="/b/99999/mobi" rel="http://opds-spec.org/acquisition/open-access"/>
  </entry>
</feed>"""

SAMPLE_OPDS_DOWNLOAD = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>PDF книга</title>
    <author><name>Автор PDF</name></author>
    <content type="text/html">Формат: pdf&lt;br/&gt;Размер: 10 Mb</content>
    <link href="/b/555/download" rel="http://opds-spec.org/acquisition/open-access"/>
    <link href="/b/555" rel="alternate"/>
  </entry>
</feed>"""


def _make_session(resp):
    sess = AsyncMock()
    sess.get = MagicMock(return_value=resp)
    return sess


def _resp_200(text):
    r = AsyncMock()
    r.status = 200
    r.text = AsyncMock(return_value=text)
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _resp_err(status):
    r = AsyncMock()
    r.status = status
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _resp_dl(content, headers=None):
    r = AsyncMock()
    r.status = 200
    r.read = AsyncMock(return_value=content)
    r.headers = headers or {}
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _patch_session(resp):
    async def fake():
        return _make_session(resp)
    return patch("bot.get_session", side_effect=fake)


# ── Search ──

class TestSearchBooks:
    @pytest.mark.asyncio
    async def test_parse(self):
        with _patch_session(_resp_200(SAMPLE_OPDS)):
            books = await flibusta_bot.search_books("тест")
        assert len(books) == 2
        assert books[0]["title"] == "Тестовая книга"
        assert books[0]["author"] == "Тестовый Автор"
        assert books[0]["id"] == "12345"
        assert set(books[0]["formats"]) == {"epub", "fb2"}
        assert "500 Kb" in books[0]["size_info"]
        assert "42 загрузок" in books[0]["size_info"]
        assert books[0]["genre"] == "Фантастика"

    @pytest.mark.asyncio
    async def test_authors(self):
        with _patch_session(_resp_200(SAMPLE_OPDS)):
            books = await flibusta_bot.search_books("тест")
        assert books[1]["author"] == "Автор Два, Автор Три"

    @pytest.mark.asyncio
    async def test_download_format_from_content(self):
        with _patch_session(_resp_200(SAMPLE_OPDS_DOWNLOAD)):
            books = await flibusta_bot.search_books("pdf")
        assert len(books) == 1
        assert books[0]["formats"] == ["pdf"]
        assert books[0]["id"] == "555"

    @pytest.mark.asyncio
    async def test_fallback_to_download(self):
        xml = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>X</title><content>text</content>
          <link href="/b/1/download" rel="http://opds-spec.org/acquisition/open-access"/></entry>
        </feed>"""
        with _patch_session(_resp_200(xml)):
            books = await flibusta_bot.search_books("x")
        assert len(books) == 1
        assert books[0]["formats"] == ["download"]

    @pytest.mark.asyncio
    async def test_empty(self):
        with _patch_session(_resp_200('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>')):
            books = await flibusta_bot.search_books("xxx")
        assert books == []

    @pytest.mark.asyncio
    async def test_http_err(self):
        with _patch_session(_resp_err(500)):
            books = await flibusta_bot.search_books("err")
        assert books == []

    @pytest.mark.asyncio
    async def test_bad_xml(self):
        with _patch_session(_resp_200("not xml")):
            books = await flibusta_bot.search_books("bad")
        assert books == []


# ── Download ──

class TestDownload:
    @pytest.mark.asyncio
    async def test_ok(self):
        with _patch_session(_resp_dl(b"x" * 1000, {"Content-Disposition": 'attachment; filename="test.fb2"'})):
            data, n = await flibusta_bot.download_book("1", "fb2")
        assert data == b"x" * 1000
        assert n == "test.fb2"

    @pytest.mark.asyncio
    async def test_no_name(self):
        with _patch_session(_resp_dl(b"x" * 200)):
            _, n = await flibusta_bot.download_book("99", "epub")
        assert n == "book_99.epub"

    @pytest.mark.asyncio
    async def test_small(self):
        with _patch_session(_resp_dl(b"x")):
            assert (await flibusta_bot.download_book("1", "fb2"))[0] is None

    @pytest.mark.asyncio
    async def test_404(self):
        with _patch_session(_resp_err(404)):
            assert (await flibusta_bot.download_book("1", "fb2"))[0] is None

    @pytest.mark.asyncio
    async def test_zip_extraction(self):
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("book.fb2", b"<fb2>content</fb2>")
        zip_data = buf.getvalue()
        with _patch_session(_resp_dl(zip_data)):
            data, name = await flibusta_bot.download_book("1", "download")
        assert data == b"<fb2>content</fb2>"
        assert name == "book.fb2"

    @pytest.mark.asyncio
    async def test_zip_skips_mimetype(self):
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr("OEBPS/content.opf", b"<opf/>")
        zip_data = buf.getvalue()
        with _patch_session(_resp_dl(zip_data)):
            data, name = await flibusta_bot.download_book("1", "download")
        assert data == b"<opf/>"
        assert name == "OEBPS/content.opf"

    @pytest.mark.asyncio
    async def test_no_zip_for_epub(self):
        epub_data = b"PK\x03\x04" + b"x" * 200
        with _patch_session(_resp_dl(epub_data)):
            data, name = await flibusta_bot.download_book("1", "epub")
        assert data == epub_data
        assert name == "book_1.epub"

    @pytest.mark.asyncio
    async def test_path_mapping(self):
        for fmt in ["fb2", "epub", "mobi", "txt", "html", "rtf"]:
            mock_r = _resp_dl(b"x" * 200)
            sess = _make_session(mock_r)
            async def fake(s=sess):
                return s
            with patch("bot.get_session", side_effect=fake):
                await flibusta_bot.download_book("1", fmt)
            url = sess.get.call_args[0][0]
            assert url.endswith(f"/{fmt}"), f"Expected /{fmt}, got {url}"

        mock_r = _resp_dl(b"x" * 200)
        sess = _make_session(mock_r)
        async def fake2(s=sess):
            return s
        with patch("bot.get_session", side_effect=fake2):
            await flibusta_bot.download_book("1", "pdf")
        url = sess.get.call_args[0][0]
        assert url.endswith("/download")


# ── Keyboards ──

class TestKeyboards:
    def test_formats(self):
        kb = flibusta_bot.build_formats_keyboard("1", ["fb2", "epub", "mobi"])
        assert len(kb.inline_keyboard) == 2
        assert kb.inline_keyboard[0][0].callback_data == "dl_1_fb2"
        assert kb.inline_keyboard[1][0].callback_data == "back_0"

    def test_formats_with_page(self):
        kb = flibusta_bot.build_formats_keyboard("1", ["fb2"], page=3)
        assert kb.inline_keyboard[1][0].callback_data == "back_3"

    def test_formats_download_only(self):
        kb = flibusta_bot.build_formats_keyboard("1", ["download"])
        assert len(kb.inline_keyboard) == 2
        assert kb.inline_keyboard[0][0].callback_data == "dl_1_download"
        assert "Скачать" in kb.inline_keyboard[0][0].text

    def test_formats_2(self):
        kb = flibusta_bot.build_formats_keyboard("1", ["fb2", "epub"])
        assert len(kb.inline_keyboard[0]) == 2

    def test_pagination(self):
        books = [{"id": str(i), "title": f"B{i}", "author": "A", "formats": ["fb2"]} for i in range(12)]
        kb = flibusta_bot._build_keyboard(books, 0)
        assert len(kb.inline_keyboard) == 6
        assert kb.inline_keyboard[5][1].callback_data == "page_1"

    def test_book_includes_page(self):
        books = [{"id": str(i), "title": f"B{i}", "author": "A", "formats": ["fb2"]} for i in range(12)]
        kb = flibusta_bot._build_keyboard(books, 2)
        assert kb.inline_keyboard[0][0].callback_data == "book_10_2"

    def test_mid_page(self):
        books = [{"id": str(i), "title": f"B{i}", "author": "A", "formats": ["fb2"]} for i in range(12)]
        kb = flibusta_bot._build_keyboard(books, 1)
        assert kb.inline_keyboard[5][0].callback_data == "page_0"
        assert kb.inline_keyboard[5][2].callback_data == "page_2"

    def test_last_page(self):
        books = [{"id": str(i), "title": f"B{i}", "author": "A", "formats": ["fb2"]} for i in range(7)]
        kb = flibusta_bot._build_keyboard(books, 1)
        assert len(kb.inline_keyboard[2]) == 2

    def test_kb_to_dict(self):
        kb = flibusta_bot.build_formats_keyboard("1", ["fb2"])
        d = flibusta_bot._kb_to_dict(kb)
        assert d["inline_keyboard"][0][0]["callback_data"] == "dl_1_fb2"


# ── Rich Markdown ──

class TestRichMarkdown:
    def test_search_with_query(self):
        books = [{"id": "1", "title": "Тест", "author": "Автор", "formats": ["fb2"], "size_info": "", "description": "", "genre": ""}]
        md = flibusta_bot.format_search_rich(books, 0, "тест")
        assert "Результаты поиска" in md
        assert "1 найдено" in md
        assert "Тест" in md
        assert "Автор" in md
        assert "`FB2`" in md

    def test_search_paged(self):
        books = [{"id": str(i), "title": f"B{i}", "author": "A", "formats": ["fb2"], "size_info": "", "description": "", "genre": ""} for i in range(12)]
        md = flibusta_bot.format_search_rich(books, 1)
        assert "Стр. 2 из 3" in md
        assert "B5" in md
        assert "B9" in md
        assert "Листайте кнопками" in md

    def test_book_detail(self):
        book = {"id": "1", "title": "Тест", "author": "А", "genre": "Фантастика", "formats": ["fb2", "epub"], "description": "Описание", "size_info": "500 Kb"}
        md = flibusta_bot.format_book_detail(book)
        assert "Тест" in md
        assert "А" in md
        assert "Фантастика" in md
        assert "500 Kb" in md
        assert "Описание" in md
        assert "**FB2**" in md
        assert "**EPUB**" in md

    def test_long_desc(self):
        book = {"id": "1", "title": "T", "author": "A", "genre": "", "formats": ["fb2"], "description": "x" * 300, "size_info": ""}
        md = flibusta_bot.format_book_detail(book)
        assert "x" * 260 in md
        assert "..." in md

    def test_formats_separator(self):
        books = [{"id": "1", "title": "T", "author": "A", "formats": ["fb2", "epub", "mobi"], "size_info": "", "description": "", "genre": ""}]
        md = flibusta_bot.format_search_rich(books, 0, "q")
        assert "·" in md

    def test_download_only_shows_button(self):
        book = {"id": "1", "title": "T", "author": "A", "genre": "", "formats": ["download"], "description": "", "size_info": ""}
        md = flibusta_bot.format_book_detail(book)
        assert "Скачать книгу" in md
        assert "FB2" not in md

    def test_fmt_formats_download_only(self):
        assert "Скачать" in flibusta_bot._fmt_formats(["download"])

    def test_fmt_formats_visible(self):
        md = flibusta_bot._fmt_formats(["fb2", "epub"])
        assert "`FB2`" in md
        assert "`EPUB`" in md
        assert "·" in md


# ── TG API ──

class TestTGApi:
    @pytest.mark.asyncio
    async def test_no_content(self):
        assert await tg_api.send_rich_message("tok", 1) is None

    @pytest.mark.asyncio
    async def test_payload(self):
        mock_r = AsyncMock()
        mock_r.json = AsyncMock(return_value={"ok": True})
        mock_r.__aenter__ = AsyncMock(return_value=mock_r)
        mock_r.__aexit__ = AsyncMock(return_value=False)
        mock_s = AsyncMock()
        mock_s.post = MagicMock(return_value=mock_r)

        async def fake():
            return mock_s

        with patch("tg_api._get_session", side_effect=fake):
            await tg_api.send_rich_message("tok", 42, markdown="# Hi", reply_markup={"inline_keyboard": []})

        import json
        data = mock_s.post.call_args[1]["data"]
        assert json.loads(data["rich_message"]) == {"markdown": "# Hi"}
        assert data["chat_id"] == 42


# ── Rate Limiter ──

class TestRateLimiter:
    def test_allows_within_limit(self):
        flibusta_bot._rate_hits.clear()
        uid = 999999
        for _ in range(5):
            assert flibusta_bot._check_rate(uid, "search") is True

    def test_blocks_over_limit(self):
        flibusta_bot._rate_hits.clear()
        uid = 999998
        for _ in range(10):
            flibusta_bot._check_rate(uid, "search")
        assert flibusta_bot._check_rate(uid, "search") is False

    def test_different_actions_independent(self):
        flibusta_bot._rate_hits.clear()
        uid = 999997
        for _ in range(10):
            flibusta_bot._check_rate(uid, "search")
        assert flibusta_bot._check_rate(uid, "download") is True

    def test_draft_counter(self):
        flibusta_bot._draft_counter.clear()
        uid = 999996
        d1 = flibusta_bot._next_draft_id(uid)
        d2 = flibusta_bot._next_draft_id(uid)
        assert d2 == d1 + 1


# ── Cache ──

class TestCache:
    def test_cleanup_removes_old_entries(self):
        flibusta_bot.user_cache[111] = ["book"]
        flibusta_bot._user_cache_time[111] = time.time() - 7200
        flibusta_bot.user_cache[222] = ["book"]
        flibusta_bot._user_cache_time[222] = time.time()
        flibusta_bot._cache_cleanup()
        assert 111 not in flibusta_bot.user_cache
        assert 222 in flibusta_bot.user_cache
        flibusta_bot.user_cache.pop(222, None)
        flibusta_bot._user_cache_time.pop(222, None)

    def test_last_query_stored(self):
        flibusta_bot.user_last_query[777] = "python"
        assert flibusta_bot.user_last_query[777] == "python"
        flibusta_bot.user_last_query.pop(777, None)


# ── Constants ──

class TestConstants:
    def test_emoji_all(self):
        for f in flibusta_bot.FORMATS:
            assert f in flibusta_bot.FORMAT_EMOJI

    def test_names(self):
        assert flibusta_bot.FORMATS["fb2"] == "FB2"
        assert flibusta_bot.FORMATS["mobi"] == "MOBI"

    def test_formats_dl(self):
        assert "fb2" in flibusta_bot._FORMATS_DL
        assert "pdf" not in flibusta_bot._FORMATS_DL
        assert "download" not in flibusta_bot._FORMATS_DL
