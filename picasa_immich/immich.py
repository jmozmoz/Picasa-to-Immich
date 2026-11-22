"""Immich REST API client. Needs the 'requests' package."""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator, Protocol

LOG = logging.getLogger(__name__)


class ImmichClient(Protocol):
    """The part of the Immich API the migration needs (implemented by ImmichAPI)."""

    def server_version(self) -> str | None: ...
    def list_assets(self, page_size: int) -> list[dict[str, Any]]: ...
    def list_albums(self) -> list[dict[str, Any]]: ...
    def album_asset_ids(self, album_id: str, page_size: int) -> set[str]: ...
    def create_album(self, name: str, asset_ids: list[str]) -> Any: ...
    def add_album_assets(self, album_id: str, asset_ids: list[str]) -> Any: ...


class ImmichAPI:
    """Client for the Immich REST API (/api), authenticated with an API key."""

    def __init__(self, url: str, api_key: str, timeout: int = 60, verify_tls: bool = True, debug: bool = False):
        try:
            import requests  # pylint: disable=import-outside-toplevel  # optional for --scan-only
        except ImportError as exc:
            raise RuntimeError(
                "The Immich migration mode requires 'requests'. Install it with: pip install requests"
            ) from exc
        self.base = url.rstrip("/") + "/api"
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": api_key, "Accept": "application/json"})
        self.session.verify = verify_tls
        self.timeout = timeout
        self.debug = debug

    def request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        """Send one request and return the decoded JSON (or text, or None for an empty response).

        Raises RuntimeError with the server's error details for HTTP errors.
        """
        url = self.base + "/" + endpoint.lstrip("/")
        if self.debug:
            # Never log the API key: only method, URL, query and JSON body.
            LOG.info(
                "API -> %s %s params=%s json=%s",
                method,
                url,
                kwargs.get("params"),
                json.dumps(kwargs.get("json"), ensure_ascii=False),
            )
        response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if self.debug:
            LOG.info("API <- HTTP %s %s (%d bytes)", response.status_code, endpoint, len(response.content))
        if not response.ok:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:2000]
            correlation = response.headers.get("X-Correlation-ID")
            raise RuntimeError(
                f"Immich API {method} {endpoint}: HTTP {response.status_code}: {detail}; "
                f"X-Correlation-ID={correlation}"
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def server_version(self) -> str | None:
        for endpoint in ("server/version", "server/about"):
            try:
                value = self.request("GET", endpoint)
            except (RuntimeError, OSError):  # HTTP error or network failure: try the next endpoint
                continue
            if isinstance(value, dict):
                return str(value.get("version") or value.get("serverVersion") or value)
            return str(value)
        return None

    @staticmethod
    def _extract_items(payload: Any) -> tuple[list[dict[str, Any]], bool]:
        """Return (items, has_more) from current and legacy search response envelopes."""
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected search response type: {type(payload).__name__}")
        candidates = [payload]
        # A bare list keeps the pagination fields of the envelope around it.
        paging = {k: payload[k] for k in ("hasNextPage", "hasMore", "nextPage") if k in payload}
        for key in ("assets", "items", "data"):
            value = payload.get(key)
            if isinstance(value, dict):
                candidates.insert(0, value)
            elif isinstance(value, list):
                candidates.insert(0, {"items": value, **paging})
        for candidate in candidates:
            items = candidate.get("items")
            if isinstance(items, list):
                has_more = bool(candidate.get("hasNextPage", candidate.get("hasMore", False)))
                if candidate.get("nextPage") not in (None, "", False):
                    has_more = True
                return items, has_more
            if isinstance(candidate.get("assets"), list):
                return candidate["assets"], bool(candidate.get("hasNextPage", False))
        raise RuntimeError(
            "Unrecognized /search/metadata response. Inspect the response and adapt pagination before applying."
        )

    @staticmethod
    def _next_cursor(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        cursor = payload.get("nextCursor")
        # Some cursor-based responses nest the cursor in "assets".
        if not cursor and isinstance(payload.get("assets"), dict):
            cursor = payload["assets"].get("nextCursor")
        return str(cursor) if cursor else None

    def search_metadata(self, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Yield every result of POST /search/metadata, following page or cursor pagination.

        Never silently truncates: an empty page that claims more results is an error.
        """
        page = 1
        cursor: str | None = None
        while True:
            request_body = {**body, **({"cursor": cursor} if cursor else {"page": page})}
            payload = self.request("POST", "search/metadata", json=request_body)
            items, has_more = self._extract_items(payload)
            yield from (item for item in items if isinstance(item, dict))
            cursor = self._next_cursor(payload)
            if cursor:
                continue
            if not has_more:
                return
            if not items:
                raise RuntimeError(
                    f"Search reports more pages but returned no items ({body}); " "refusing an incomplete result."
                )
            page += 1
            if page > 1_000_000:
                raise RuntimeError("Pagination safety limit reached.")

    def list_assets(self, page_size: int = 1000) -> list[dict[str, Any]]:
        # withExif is needed for exifInfo.fileSizeInByte, used by size matching.
        unique: dict[str, dict[str, Any]] = {}
        for item in self.search_metadata({"size": page_size, "withExif": True}):
            if item.get("id"):
                unique.setdefault(item["id"], item)
        return list(unique.values())

    def list_albums(self) -> list[dict[str, Any]]:
        result = self.request("GET", "albums", params={"isOwned": "true"})
        albums = result.get("items") if isinstance(result, dict) else result
        if isinstance(albums, list):
            return [album for album in albums if isinstance(album, dict)]
        raise RuntimeError(f"Unexpected GET /albums response: {type(result).__name__}")

    def album_asset_ids(self, album_id: str, page_size: int = 1000) -> set[str]:
        body = {"size": page_size, "albumIds": [album_id]}
        return {str(item["id"]) for item in self.search_metadata(body) if item.get("id")}

    def create_album(self, name: str, asset_ids: list[str]) -> Any:
        return self.request("POST", "albums", json={"albumName": name, "assetIds": asset_ids})

    def add_album_assets(self, album_id: str, asset_ids: list[str]) -> Any:
        return self.request("PUT", f"albums/{album_id}/assets", json={"ids": asset_ids})
