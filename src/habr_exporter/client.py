"""HTTP client for the public Habr JSON API (``/kek/v2``).

Habr renders article pages server-side, but the same data is available as JSON at
``https://habr.com/kek/v2/...``. Working with JSON is more reliable than parsing markup that
changes regularly.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .i18n import tr

API_ROOT = "https://habr.com/kek/v2"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 HabrExporter/1.0"
)


class HabrError(RuntimeError):
    """Failed to get data from Habr."""


class ArticleNotFoundError(HabrError):
    """The article (or user, or list page) does not exist, was deleted or is hidden."""

    def __init__(self, message: str, api_message: str = "") -> None:
        super().__init__(message)
        #: Explanation from the API response, e.g. ``Page is out of range``.
        self.api_message = api_message


@dataclass(slots=True)
class Resource:
    """Downloaded binary resource (an image)."""

    url: str
    content: bytes
    content_type: str


class HabrClient:
    """Thin wrapper around ``requests.Session`` with retries and timeouts."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        timeout: float = 20.0,
        lang: str = "ru",
        min_interval: float = 0.0,
    ) -> None:
        self.timeout = timeout
        self.lang = lang
        #: Minimum delay between JSON API requests, in seconds (0 means no delay).
        self.min_interval = min_interval
        self.session = session or self._make_session()
        self._last_request: float | None = None
        self._throttle_lock = threading.Lock()

    @staticmethod
    def _make_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"})
        return session

    # --- Request throttling ------------------------------------------------------

    @contextmanager
    def throttled(self, interval: float) -> Iterator[None]:
        """Temporarily enforce a delay of at least ``interval`` seconds between API requests."""
        previous = self.min_interval
        self.min_interval = max(previous, interval)
        try:
            yield
        finally:
            self.min_interval = previous

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        with self._throttle_lock:
            if self._last_request is not None:
                wait = self._last_request + self.min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            self._last_request = time.monotonic()

    # --- JSON API -----------------------------------------------------------------

    def _get_json(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{API_ROOT}/{path.strip('/')}/"
        params = {**(query or {}), "fl": self.lang, "hl": self.lang}
        self._throttle()
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise HabrError(tr("error.network", url=url, error=exc)) from exc
        if response.status_code == 404:
            api_message = _api_message(response)
            details = f" ({api_message})" if api_message else ""
            raise ArticleNotFoundError(tr("error.not_found", url=url, details=details), api_message)
        if response.status_code >= 400:
            raise HabrError(tr("error.http", status=response.status_code, url=url))
        try:
            data = response.json()
        except ValueError as exc:
            raise HabrError(tr("error.bad_json", url=url)) from exc
        if not isinstance(data, dict):
            raise HabrError(tr("error.unexpected", url=url))
        return data

    def get_article(self, article_id: str) -> dict[str, Any]:
        return self._get_json(f"articles/{article_id}")

    def get_comments(self, article_id: str) -> dict[str, Any]:
        return self._get_json(f"articles/{article_id}/comments")

    def get_user_card(self, alias: str) -> dict[str, Any]:
        return self._get_json(f"users/{alias}/card")

    def get_company_card(self, alias: str) -> dict[str, Any]:
        return self._get_json(f"companies/{alias}/card")

    def get_user_articles(
        self,
        alias: str,
        page: int = 1,
        *,
        bookmarks: bool = False,
        news: bool = False,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """Return a page of a user's publication list.

        The result has ``pagesCount``, ``publicationIds`` and ``publicationRefs``.
        ``bookmarks=True`` returns the user's public bookmarks, otherwise their own articles
        (``news=True`` returns their news, which come as a separate list). ``per_page`` is at
        most 100.
        """
        query: dict[str, Any] = {"user": alias, "page": page, "perPage": per_page}
        if bookmarks:
            query["user_bookmarks"] = "true"
        if news:
            query["news"] = "true"
        return self._get_json("articles", query)

    # --- Binary resources ---------------------------------------------------------

    def download(self, url: str) -> Resource:
        """Download a whole resource. Raise :class:`HabrError` on failure."""
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise HabrError(tr("error.download", url=url, error=exc)) from exc
        if response.status_code >= 400:
            raise HabrError(tr("error.download_http", status=response.status_code, url=url))
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        return Resource(url=url, content=response.content, content_type=content_type)


def _api_message(response: Any) -> str:
    """Return the ``message`` text from a JSON error response, if any."""
    try:
        data = response.json()
    except ValueError:
        return ""
    message = data.get("message") if isinstance(data, dict) else None
    return message if isinstance(message, str) else ""
