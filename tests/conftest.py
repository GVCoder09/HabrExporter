"""Shared fixtures: synthetic Habr API responses, a fake HTTP session, images."""

from __future__ import annotations

import copy
import io
import json
import os
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

# Qt WebEngine needs the module imported and the attribute set before QApplication is created (by
# pytest-qt).
import PyQt6.QtWebEngineWidgets  # noqa: F401
from PyQt6.QtCore import QCoreApplication, Qt

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

from PyQt6.QtCore import QLocale  # noqa: E402

from habr_exporter import i18n  # noqa: E402
from habr_exporter.client import HabrClient  # noqa: E402
from habr_exporter.models import Article  # noqa: E402
from habr_exporter.parser import parse_article  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

IMG_FIGURE = "https://habrastorage.org/r/w1560/getpro/habr/upload_files/aa/bb/cc/figure.png"
IMG_LAZY = "https://habrastorage.org/webt/lazy/picture.webp"
IMG_FORMULA = "https://habrastorage.org/getpro/habr/formulas/formula.svg"
IMG_COMMENT = "https://habrastorage.org/webt/comment/picture.jpg"
IMG_BROKEN = "https://habrastorage.org/webt/broken.png"
AVATAR_AUTHOR = "https://habrastorage.org/getpro/habr/avatars/author.png"
AVATAR_ALICE = "https://habrastorage.org/getpro/habr/avatars/alice.png"
COMPANY_LOGO = "https://habrastorage.org/getpro/habr/company/logo.png"

SVG = (
    b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="40" height="12">'
    b'<rect width="40" height="12" fill="#333"/></svg>'
)


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_png(width: int = 40, height: int = 30, color: str = "#65a3be", fmt: str = "PNG") -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, fmt)
    return out.getvalue()


# --- Fake HTTP --------------------------------------------------------------------


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        content: bytes = b"",
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._payload = payload
        self.content = content if payload is None else json.dumps(payload).encode()
        self.headers = headers or {}

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Replaces ``requests.Session``, answering from a predefined URL table.

    Request parameters other than the language ones (``fl``/``hl``) are part of the table key as a
    query string with sorted keys: ``.../articles/?page=1&perPage=100&user=alice``.
    """

    def __init__(self, routes: dict[str, FakeResponse | Callable[[], FakeResponse]]):
        self.routes = routes
        self.requests: list[str] = []
        self.headers: dict[str, str] = {}

    def get(
        self, url: str, params: dict | None = None, timeout: float | None = None
    ) -> FakeResponse:
        query = {k: v for k, v in (params or {}).items() if k not in ("fl", "hl")}
        if query:
            url += "?" + urlencode(sorted(query.items()))
        self.requests.append(url)
        route = self.routes.get(url)
        if route is None:
            return FakeResponse(404, content=b"not found")
        return route() if callable(route) else route


API = "https://habr.com/kek/v2"


@pytest.fixture(autouse=True)
def russian_ui():
    """Keep tests independent of the system language.

    Russian UI by default, reset after each test.
    """
    previous = QLocale()
    QLocale.setDefault(QLocale(QLocale.Language.Russian, QLocale.Country.Russia))
    i18n.set_language(i18n.Language.RU)
    yield
    i18n.set_language(i18n.Language.RU)
    QLocale.setDefault(previous)


@pytest.fixture
def article_json() -> dict[str, Any]:
    return load_fixture("article.json")


@pytest.fixture
def comments_json() -> dict[str, Any]:
    return load_fixture("comments.json")


@pytest.fixture
def user_card_json() -> dict[str, Any]:
    return load_fixture("user_card.json")


@pytest.fixture
def company_card_json() -> dict[str, Any]:
    return load_fixture("company_card.json")


@pytest.fixture
def image_routes() -> dict[str, FakeResponse]:
    return {
        IMG_FIGURE: FakeResponse(
            content=make_png(1560, 600), headers={"Content-Type": "image/png"}
        ),
        IMG_LAZY: FakeResponse(
            content=make_png(300, 200, fmt="WEBP"), headers={"Content-Type": "image/webp"}
        ),
        IMG_FORMULA: FakeResponse(content=SVG, headers={"Content-Type": "image/svg+xml"}),
        IMG_COMMENT: FakeResponse(
            content=make_png(64, 64, fmt="JPEG"), headers={"Content-Type": "image/jpeg"}
        ),
        IMG_BROKEN: FakeResponse(500),
        AVATAR_AUTHOR: FakeResponse(
            content=make_png(48, 48, "#a0522d"), headers={"Content-Type": "image/png"}
        ),
        AVATAR_ALICE: FakeResponse(
            content=make_png(48, 48, "#2e8b57"), headers={"Content-Type": "image/png"}
        ),
        COMPANY_LOGO: FakeResponse(
            content=make_png(48, 48, "#000"), headers={"Content-Type": "image/png"}
        ),
    }


@pytest.fixture
def fake_session(
    article_json, comments_json, user_card_json, company_card_json, image_routes
) -> FakeSession:
    routes: dict[str, Any] = {
        f"{API}/articles/1000001/": FakeResponse(payload=article_json),
        f"{API}/articles/1000001/comments/": FakeResponse(payload=comments_json),
        f"{API}/users/test_author/card/": FakeResponse(payload=user_card_json),
        f"{API}/companies/acme/card/": FakeResponse(payload=company_card_json),
        **image_routes,
    }
    return FakeSession(routes)


@pytest.fixture
def client(fake_session: FakeSession) -> HabrClient:
    return HabrClient(session=fake_session)  # type: ignore[arg-type]


@pytest.fixture
def article(article_json, comments_json, user_card_json, company_card_json) -> Article:
    return parse_article(
        copy.deepcopy(article_json),
        comments=comments_json,
        author_card=user_card_json,
        company_card=company_card_json,
    )


@pytest.fixture
def utc():
    return UTC


# --- Batch export ------------------------------------------------------------------

BATCH_IDS = ("1000001", "1000002", "1000003")


def listing_url(user: str, page: int, *, bookmarks: bool = False, news: bool = False) -> str:
    """Return a publication list page URL as :class:`FakeSession` records it."""
    query: dict[str, Any] = {"user": user, "page": page, "perPage": 100}
    if bookmarks:
        query["user_bookmarks"] = "true"
    if news:
        query["news"] = "true"
    return f"{API}/articles/?" + urlencode(sorted(query.items()))


def listing_page(
    ids: list[str], pages: int, dates: dict[str, str | None] | None = None
) -> FakeResponse:
    """Return a list page.

    ``dates`` overrides publication dates (March 5, 2026 for all by default).
    """
    dates = dates or {}
    refs = {
        i: {
            "id": i,
            "titleHtml": f"Статья &laquo;{i}&raquo;",
            "lang": "ru",
            "timePublished": dates.get(i, "2026-03-05T09:30:00+00:00"),
            "author": {"alias": "test_author"},
        }
        for i in ids
    }
    return FakeResponse(
        payload={"pagesCount": pages, "publicationIds": ids, "publicationRefs": refs}
    )


def out_of_range() -> FakeResponse:
    payload = {
        "httpCode": 404,
        "message": "Page is out of range",
        "data": {},
        "errorCode": "NOT_FOUND",
    }
    return FakeResponse(404, payload=payload)


@pytest.fixture
def batch_session(fake_session, article_json, comments_json) -> FakeSession:
    """Serve three articles (1000001-1000003) bookmarked by user ``alice`` on two list pages."""
    for article_id in BATCH_IDS[1:]:
        data = copy.deepcopy(article_json)
        data["id"] = article_id
        data["titleHtml"] = f"Статья номер {article_id}"
        fake_session.routes[f"{API}/articles/{article_id}/"] = FakeResponse(payload=data)
        fake_session.routes[f"{API}/articles/{article_id}/comments/"] = FakeResponse(
            payload=comments_json
        )
    fake_session.routes[listing_url("alice", 1, bookmarks=True)] = listing_page(
        list(BATCH_IDS[:2]), 2
    )
    fake_session.routes[listing_url("alice", 2, bookmarks=True)] = listing_page([BATCH_IDS[2]], 2)
    return fake_session
