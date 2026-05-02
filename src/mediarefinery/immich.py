from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic, sleep
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen
import json
import os
import random
import ssl


DEFAULT_API_KEY_ENV = "IMMICH_API_KEY"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 0.25
IMMICH_API_KEY_HEADER = "x-api-key"


@dataclass(frozen=True)
class AssetRef:
    asset_id: str
    media_type: str
    checksum: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    albums: tuple[str, ...] = ()
    archived: bool = False
    favorite: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ImmichCapabilities:
    albums: bool = True
    tags: bool = False
    archive: bool = False
    locked_folder: bool = False


class ImmichClientError(Exception):
    """Safe Immich HTTP client error without response bodies or secret values."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "immich_request_failed",
        status_code: int | None = None,
    ):
        self.error_code = error_code
        self.status_code = status_code
        if status_code is not None:
            message = f"{message} (status {status_code})"
        super().__init__(message)


class ImmichClientConfigurationError(ImmichClientError):
    """Raised when the HTTP adapter cannot be constructed from safe config/env."""


SYNTHETIC_IMAGE_PREVIEW_BYTES = bytes(
    [
        137,
        80,
        78,
        71,
        13,
        10,
        26,
        10,
        0,
        0,
        0,
        13,
        73,
        72,
        68,
        82,
        0,
        0,
        0,
        1,
        0,
        0,
        0,
        1,
        8,
        6,
        0,
        0,
        0,
        31,
        21,
        196,
        137,
        0,
        0,
        0,
        10,
        73,
        68,
        65,
        84,
        120,
        156,
        99,
        0,
        1,
        0,
        0,
        5,
        0,
        1,
        13,
        10,
        45,
        180,
        0,
        0,
        0,
        0,
        73,
        69,
        78,
        68,
        174,
        66,
        96,
        130,
    ]
)


class ImmichClient(Protocol):
    @property
    def capabilities(self) -> ImmichCapabilities:
        ...

    def list_assets(
        self,
        page_token: str | None = None,
        page_size: int = 100,
        media_types: set[str] | None = None,
    ) -> tuple[list[AssetRef], str | None]:
        ...

    def get_metadata(self, asset_id: str) -> dict[str, Any]:
        ...

    def get_preview_bytes(self, asset_id: str) -> bytes:
        ...

    def find_album_by_name(self, name: str) -> str | None:
        ...

    def create_album(self, name: str) -> str:
        ...

    def create_or_get_album(self, name: str) -> str:
        ...

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        ...

    def find_tag_by_name(self, name: str) -> str | None:
        ...

    def create_or_get_tag(self, name: str) -> str:
        ...

    def create_tag(self, name: str) -> str:
        ...

    def add_tag_to_asset(self, asset_id: str, tag_id: str) -> None:
        ...

    def archive_asset(self, asset_id: str) -> None:
        ...

    def set_asset_visibility(self, asset_id: str, visibility: str) -> None:
        ...


class HttpImmichClient:
    """Minimal Immich v2.x HTTP adapter for reads, albums, and tags."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        verify_tls: bool = True,
        rate_limit_per_second: float | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        urlopen_func: Callable[..., Any] | None = None,
        sleep_func: Callable[[float], None] = sleep,
    ):
        if not base_url or not str(base_url).strip():
            raise ImmichClientConfigurationError(
                "Immich base URL is not configured",
                error_code="missing_base_url",
            )
        if not api_key or not str(api_key).strip():
            raise ImmichClientConfigurationError(
                "Immich API key env var is not set",
                error_code="missing_api_key",
            )

        self._base_url = str(base_url).strip()
        self._api_key = str(api_key)
        self._timeout_seconds = _positive_float(
            timeout_seconds,
            DEFAULT_TIMEOUT_SECONDS,
        )
        self._context = None if verify_tls else ssl._create_unverified_context()
        self._urlopen = urlopen_func or urlopen
        self._sleep = sleep_func
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self._rate_limit_interval_seconds = _rate_limit_interval(
            rate_limit_per_second
        )
        self._last_request_at = 0.0

    @property
    def capabilities(self) -> ImmichCapabilities:
        return ImmichCapabilities(
            albums=True,
            tags=True,
            archive=False,
            locked_folder=True,
        )

    def ping_server(self) -> Mapping[str, Any]:
        data = self._request_json("GET", "/server/ping", authenticated=False)
        return _mapping_or_empty(data)

    def server_version(self) -> Mapping[str, Any]:
        data = self._request_json("GET", "/server/version", authenticated=False)
        return _mapping_or_empty(data)

    def about(self) -> Mapping[str, Any]:
        data = self._request_json("GET", "/server/about")
        return _mapping_or_empty(data)

    def features(self) -> Mapping[str, Any]:
        data = self._request_json("GET", "/server/features", authenticated=False)
        return _mapping_or_empty(data)

    def list_assets(
        self,
        page_token: str | None = None,
        page_size: int = 100,
        media_types: set[str] | None = None,
    ) -> tuple[list[AssetRef], str | None]:
        page = _page_from_token(page_token)
        body: dict[str, object] = {
            "page": page,
            "size": _positive_int(page_size, 100),
            "withDeleted": False,
            "withExif": False,
            "withPeople": False,
            "withStacked": False,
        }
        asset_type = _immich_asset_type_filter(media_types)
        if asset_type is not None:
            body["type"] = asset_type

        data = self._request_json("POST", "/search/metadata", json_body=body)
        assets_page = _search_assets_page(data)
        items = assets_page.get("items") or []
        if not isinstance(items, list):
            raise ImmichClientError(
                "Immich asset search response was not valid",
                error_code="invalid_asset_search_response",
            )
        assets = [
            _asset_ref_from_response(item)
            for item in items
            if isinstance(item, Mapping)
        ]
        return assets, _next_page_token(assets_page.get("nextPage"))

    def get_metadata(self, asset_id: str) -> dict[str, Any]:
        data = self._request_json(
            "POST",
            "/search/metadata",
            json_body={
                "id": asset_id,
                "page": 1,
                "size": 1,
                "withDeleted": False,
                "withExif": False,
                "withPeople": False,
                "withStacked": False,
            },
        )
        assets_page = _search_assets_page(data)
        items = assets_page.get("items") or []
        if not isinstance(items, list) or not items or not isinstance(items[0], Mapping):
            raise KeyError(asset_id)
        return _metadata_from_asset_response(items[0])

    def get_preview_bytes(self, asset_id: str) -> bytes:
        return self._request_bytes(
            "GET",
            f"/assets/{quote(asset_id, safe='')}/thumbnail",
            query={"size": "preview"},
        )

    def find_album_by_name(self, name: str) -> str | None:
        data = self._request_json("GET", "/albums")
        if not isinstance(data, list):
            raise ImmichClientError(
                "Immich albums response was not valid",
                error_code="invalid_album_response",
            )
        for item in data:
            if not isinstance(item, Mapping):
                continue
            if item.get("albumName") == name and isinstance(item.get("id"), str):
                return str(item["id"])
        return None

    def create_album(self, name: str) -> str:
        data = self._request_json(
            "POST",
            "/albums",
            json_body={"albumName": name},
            expected_statuses={200, 201},
        )
        if isinstance(data, Mapping) and isinstance(data.get("id"), str):
            return str(data["id"])
        raise ImmichClientError(
            "Immich create album response was not valid",
            error_code="invalid_album_response",
        )

    def create_or_get_album(self, name: str) -> str:
        album_id = self.find_album_by_name(name)
        if album_id is not None:
            return album_id
        return self.create_album(name)

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        if not asset_ids:
            return
        data = self._request_json(
            "PUT",
            f"/albums/{quote(album_id, safe='')}/assets",
            json_body={"ids": list(asset_ids)},
        )
        if not isinstance(data, list):
            raise ImmichClientError(
                "Immich add-to-album response was not valid",
                error_code="invalid_album_response",
            )
        failures = [
            item
            for item in data
            if isinstance(item, Mapping)
            and item.get("success") is not True
            and item.get("error") != "duplicate"
        ]
        if failures:
            raise ImmichClientError(
                "Immich did not add one or more assets to the album",
                error_code="album_add_failed",
            )

    def find_tag_by_name(self, name: str) -> str | None:
        tag_name = str(name).strip()
        if not tag_name:
            return None

        data = self._request_json("GET", "/tags")
        if not isinstance(data, list):
            raise ImmichClientError(
                "Immich tags response was not valid",
                error_code="invalid_tag_response",
            )
        for item in data:
            if not isinstance(item, Mapping):
                continue
            if _tag_name_matches(item, tag_name) and isinstance(item.get("id"), str):
                return str(item["id"])
        return None

    def create_or_get_tag(self, name: str) -> str:
        tag_id = self.find_tag_by_name(name)
        if tag_id is not None:
            return tag_id
        return self.create_tag(name)

    def create_tag(self, name: str) -> str:
        tag_name = str(name).strip()
        if not tag_name:
            raise ImmichClientError(
                "Immich tag name was not configured",
                error_code="tag_name_missing",
            )

        data = self._request_json(
            "POST",
            "/tags",
            json_body={"name": tag_name},
            expected_statuses={200, 201},
        )
        if isinstance(data, Mapping) and isinstance(data.get("id"), str):
            return str(data["id"])
        raise ImmichClientError(
            "Immich create tag response was not valid",
            error_code="invalid_tag_response",
        )

    def add_tag_to_asset(self, asset_id: str, tag_id: str) -> None:
        if not asset_id:
            return
        data = self._request_json(
            "PUT",
            f"/tags/{quote(tag_id, safe='')}/assets",
            json_body={"ids": [asset_id]},
        )
        if not isinstance(data, list):
            raise ImmichClientError(
                "Immich add-to-tag response was not valid",
                error_code="invalid_tag_response",
            )
        failures = [
            item
            for item in data
            if isinstance(item, Mapping)
            and item.get("success") is not True
            and item.get("error") != "duplicate"
        ]
        if failures:
            raise ImmichClientError(
                "Immich did not add one or more assets to the tag",
                error_code="tag_add_failed",
            )

    def archive_asset(self, asset_id: str) -> None:
        raise NotImplementedError("archive actions are unsupported by the HTTP adapter")

    def set_asset_visibility(self, asset_id: str, visibility: str) -> None:
        # Phase A discovery (Immich v2.7.5): per-asset PUT /assets/{id}
        # with body {"visibility": "locked" | "timeline"}. Bulk PUT is
        # not supported on this version; the runner walks ids one at a
        # time. Forward writes (visibility="locked") work with the
        # x-api-key auth path used by HttpImmichClient. The reverse
        # ("timeline") requires a PIN-unlocked Bearer session — see
        # PR 4.
        if not asset_id:
            raise ValueError("asset_id is required")
        if visibility not in {"locked", "timeline"}:
            raise ValueError(
                "visibility must be 'locked' or 'timeline'"
            )
        self._request_json(
            "PUT",
            f"/assets/{quote(asset_id, safe='')}",
            json_body={"visibility": visibility},
        )

    def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        authenticated: bool = True,
        json_body: Mapping[str, object] | None = None,
        query: Mapping[str, str] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> object:
        body = self._request_bytes(
            method,
            endpoint,
            authenticated=authenticated,
            json_body=json_body,
            query=query,
            expected_statuses=expected_statuses,
            accept="application/json",
        )
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImmichClientError(
                "Immich response was not valid JSON",
                error_code="invalid_json_response",
            ) from exc

    def _request_bytes(
        self,
        method: str,
        endpoint: str,
        *,
        authenticated: bool = True,
        json_body: Mapping[str, object] | None = None,
        query: Mapping[str, str] | None = None,
        expected_statuses: set[int] | None = None,
        accept: str = "application/octet-stream",
    ) -> bytes:
        expected = expected_statuses or {200}
        headers = {"Accept": accept}
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers[IMMICH_API_KEY_HEADER] = self._api_key

        request = Request(
            _immich_api_url(self._base_url, endpoint, query=query),
            data=data,
            headers=headers,
            method=method,
        )

        attempts = self._max_retries + 1
        for attempt in range(attempts):
            self._pace_request()
            try:
                with self._urlopen(
                    request,
                    timeout=self._timeout_seconds,
                    context=self._context,
                ) as response:
                    body = response.read()
                    status_code = int(response.status)
            except HTTPError as exc:
                status_code = int(exc.code)
                if _should_retry(status_code) and attempt < attempts - 1:
                    self._sleep_retry(attempt)
                    continue
                raise _http_error(status_code) from exc
            except (OSError, TimeoutError, URLError) as exc:
                if attempt < attempts - 1:
                    self._sleep_retry(attempt)
                    continue
                raise ImmichClientError(
                    "Immich HTTP request failed",
                    error_code="network_unreachable",
                ) from exc

            if status_code in expected:
                return body
            if _should_retry(status_code) and attempt < attempts - 1:
                self._sleep_retry(attempt)
                continue
            raise _http_error(status_code)

        raise ImmichClientError(
            "Immich HTTP request failed",
            error_code="request_failed",
        )

    def _pace_request(self) -> None:
        if self._rate_limit_interval_seconds <= 0:
            return
        now = monotonic()
        wait_seconds = self._last_request_at + self._rate_limit_interval_seconds - now
        if wait_seconds > 0:
            self._sleep(wait_seconds)
        self._last_request_at = monotonic()

    def _sleep_retry(self, attempt: int) -> None:
        if self._retry_backoff_seconds <= 0:
            return
        delay = self._retry_backoff_seconds * (2**attempt)
        delay += random.uniform(0.0, self._retry_backoff_seconds)
        self._sleep(delay)


class MockImmichClient:
    """Deterministic no-network client for tests and dry runs."""

    def __init__(
        self,
        assets: list[AssetRef] | None = None,
        *,
        capabilities: ImmichCapabilities | None = None,
        preview_bytes_by_asset_id: Mapping[str, bytes | None] | None = None,
    ):
        self._assets = list(assets) if assets is not None else list(mock_assets())
        self._capabilities = capabilities or ImmichCapabilities()
        self._preview_bytes_by_asset_id = dict(preview_bytes_by_asset_id or {})
        self._album_ids_by_name: dict[str, str] = {}
        self._album_assets: dict[str, set[str]] = {}
        self._tag_ids_by_name: dict[str, str] = {}
        self._asset_tags: dict[str, set[str]] = {}
        self._archived_asset_ids: set[str] = set()
        self._locked_asset_ids: set[str] = set()
        self.visibility_requests: list[dict[str, str]] = []
        self.list_requests: list[dict[str, Any]] = []
        self.metadata_requests: list[str] = []
        self.preview_requests: list[str] = []
        self.album_find_requests: list[str] = []
        self.album_create_requests: list[str] = []
        self.add_to_album_requests: list[dict[str, Any]] = []
        self.tag_find_requests: list[str] = []
        self.tag_create_requests: list[str] = []
        self.add_tag_requests: list[dict[str, str]] = []
        self.archive_requests: list[str] = []

    @property
    def capabilities(self) -> ImmichCapabilities:
        return self._capabilities

    def list_assets(
        self,
        page_token: str | None = None,
        page_size: int = 100,
        media_types: set[str] | None = None,
    ) -> tuple[list[AssetRef], str | None]:
        if page_size < 1:
            raise ValueError("page_size must be positive")

        self.list_requests.append(
            {
                "page_token": page_token,
                "page_size": page_size,
                "media_types": set(media_types) if media_types is not None else None,
            }
        )
        start = int(page_token or "0")
        filtered = [
            asset
            for asset in self._assets
            if media_types is None or asset.media_type in media_types
        ]
        end = start + page_size
        next_token = str(end) if end < len(filtered) else None
        return filtered[start:end], next_token

    def get_metadata(self, asset_id: str) -> dict[str, Any]:
        self.metadata_requests.append(asset_id)
        asset = self._find(asset_id)
        return {
            "asset_id": asset.asset_id,
            "media_type": asset.media_type,
            "checksum": asset.checksum,
            "metadata": dict(asset.metadata),
            "albums": list(asset.albums),
            "archived": asset.archived,
            "favorite": asset.favorite,
            "created_at": asset.created_at.isoformat() if asset.created_at else None,
            "updated_at": asset.updated_at.isoformat() if asset.updated_at else None,
        }

    def get_preview_bytes(self, asset_id: str) -> bytes:
        self.preview_requests.append(asset_id)
        self._find(asset_id)
        if asset_id in self._preview_bytes_by_asset_id:
            preview_bytes = self._preview_bytes_by_asset_id[asset_id]
            return preview_bytes or b""
        return SYNTHETIC_IMAGE_PREVIEW_BYTES

    def find_album_by_name(self, name: str) -> str | None:
        self.album_find_requests.append(name)
        return self._album_ids_by_name.get(name)

    def create_album(self, name: str) -> str:
        self.album_create_requests.append(name)
        existing_album_id = self._album_ids_by_name.get(name)
        if existing_album_id is not None:
            return existing_album_id

        album_id = f"mock-album-{len(self._album_ids_by_name) + 1}"
        self._album_ids_by_name[name] = album_id
        self._album_assets[album_id] = set()
        return album_id

    def create_or_get_album(self, name: str) -> str:
        album_id = self.find_album_by_name(name)
        if album_id is not None:
            return album_id
        return self.create_album(name)

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        if album_id not in self._album_assets:
            raise KeyError(album_id)
        for asset_id in asset_ids:
            self._find(asset_id)
        self.add_to_album_requests.append(
            {"album_id": album_id, "asset_ids": list(asset_ids)}
        )
        self._album_assets[album_id].update(asset_ids)

    def find_tag_by_name(self, name: str) -> str | None:
        self.tag_find_requests.append(name)
        return self._tag_ids_by_name.get(name)

    def create_or_get_tag(self, name: str) -> str:
        if not self.capabilities.tags:
            raise NotImplementedError("tag actions are unsupported")
        tag_id = self.find_tag_by_name(name)
        if tag_id is not None:
            return tag_id
        return self.create_tag(name)

    def create_tag(self, name: str) -> str:
        if not self.capabilities.tags:
            raise NotImplementedError("tag actions are unsupported")
        self.tag_create_requests.append(name)
        tag_id = f"mock-tag-{len(self._tag_ids_by_name) + 1}"
        self._tag_ids_by_name[name] = tag_id
        return tag_id

    def add_tag_to_asset(self, asset_id: str, tag_id: str) -> None:
        if not self.capabilities.tags:
            raise NotImplementedError("tag actions are unsupported")
        self._find(asset_id)
        if tag_id not in set(self._tag_ids_by_name.values()):
            raise KeyError(tag_id)
        self.add_tag_requests.append({"asset_id": asset_id, "tag_id": tag_id})
        self._asset_tags.setdefault(asset_id, set()).add(tag_id)

    def archive_asset(self, asset_id: str) -> None:
        if not self.capabilities.archive:
            raise NotImplementedError("archive actions are unsupported")
        self._find(asset_id)
        self.archive_requests.append(asset_id)
        self._archived_asset_ids.add(asset_id)

    def set_asset_visibility(self, asset_id: str, visibility: str) -> None:
        if not self.capabilities.locked_folder:
            raise NotImplementedError("locked-folder actions are unsupported")
        if visibility not in {"locked", "timeline"}:
            raise ValueError("visibility must be 'locked' or 'timeline'")
        self._find(asset_id)
        self.visibility_requests.append(
            {"asset_id": asset_id, "visibility": visibility}
        )
        if visibility == "locked":
            self._locked_asset_ids.add(asset_id)
        else:
            self._locked_asset_ids.discard(asset_id)

    def album_assets(self, name: str) -> tuple[str, ...]:
        album_id = self._album_ids_by_name.get(name)
        if album_id is None:
            return ()
        return tuple(sorted(self._album_assets[album_id]))

    def asset_tags(self, asset_id: str) -> tuple[str, ...]:
        return tuple(sorted(self._asset_tags.get(asset_id, set())))

    def archived_asset_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._archived_asset_ids))

    def _find(self, asset_id: str) -> AssetRef:
        for asset in self._assets:
            if asset.asset_id == asset_id:
                return asset
        raise KeyError(asset_id)


def mock_assets() -> tuple[AssetRef, ...]:
    """Default mock library covering scanner filters without media bytes."""

    return (
        AssetRef(
            asset_id="mock-image-001",
            media_type="image",
            checksum="sha256:001",
            metadata={"mock_raw_label": "raw_safety"},
            albums=("camera-roll", "family"),
            favorite=True,
            created_at=_utc(2026, 1, 1, 10),
            updated_at=_utc(2026, 1, 1, 10),
        ),
        AssetRef(
            asset_id="mock-image-002",
            media_type="image",
            checksum="sha256:002",
            metadata={"mock_raw_label": "raw_flag"},
            albums=("camera-roll", "projects"),
            created_at=_utc(2026, 1, 2, 11),
            updated_at=_utc(2026, 1, 2, 11),
        ),
        AssetRef(
            asset_id="mock-image-003",
            media_type="image",
            checksum="sha256:003",
            metadata={"mock_raw_label": "raw_safety"},
            albums=("travel",),
            favorite=True,
            created_at=_utc(2026, 1, 3, 12),
            updated_at=_utc(2026, 1, 3, 12),
        ),
        AssetRef(
            asset_id="mock-video-001",
            media_type="video",
            checksum="sha256:004",
            metadata={"mock_raw_label": "raw_safety"},
            albums=("camera-roll", "clips"),
            created_at=_utc(2026, 1, 4, 13),
            updated_at=_utc(2026, 1, 4, 13),
        ),
        AssetRef(
            asset_id="mock-image-archived-001",
            media_type="image",
            checksum="sha256:005",
            metadata={"mock_raw_label": "raw_flag"},
            albums=("archive",),
            archived=True,
            created_at=_utc(2026, 1, 5, 14),
            updated_at=_utc(2026, 1, 5, 14),
        ),
        AssetRef(
            asset_id="mock-video-favorite-001",
            media_type="video",
            checksum="sha256:006",
            metadata={"mock_raw_label": "raw_flag"},
            albums=("clips", "favorites"),
            favorite=True,
            created_at=_utc(2026, 1, 6, 15),
            updated_at=_utc(2026, 1, 6, 15),
        ),
    )


def _utc(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def create_http_immich_client(
    config: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> HttpImmichClient:
    immich = _immich_config(config)
    api_key_env = _api_key_env_name(immich)
    env = environ if environ is not None else os.environ
    api_key = env.get(api_key_env)
    if not api_key:
        raise ImmichClientConfigurationError(
            f"required env var {api_key_env} is not set",
            error_code="missing_api_key",
        )

    return HttpImmichClient(
        base_url=str(immich.get("url") or ""),
        api_key=api_key,
        timeout_seconds=_positive_float(
            immich.get("timeout_seconds"),
            DEFAULT_TIMEOUT_SECONDS,
        ),
        verify_tls=bool(immich.get("verify_tls", True)),
        rate_limit_per_second=_optional_float(
            (getattr(config, "runtime", {}) or {}).get("rate_limit_per_second")
        ),
    )


def _immich_config(config: Any) -> dict[str, object]:
    raw = getattr(config, "raw", {}) or {}
    return dict(((raw.get("integration") or {}).get("immich") or {}))


def _api_key_env_name(immich: Mapping[str, object]) -> str:
    value = immich.get("api_key_env")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_API_KEY_ENV


def _immich_api_url(
    base_url: str,
    endpoint: str,
    *,
    query: Mapping[str, str] | None = None,
) -> str:
    parsed = urlparse(base_url)
    base_path = parsed.path.rstrip("/")
    api_path = base_path if base_path.endswith("/api") else f"{base_path}/api"
    endpoint_path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    query_text = urlencode(dict(query or {}))
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            f"{api_path}{endpoint_path}",
            "",
            query_text,
            "",
        )
    )


def _page_from_token(page_token: str | None) -> int:
    if page_token is None or page_token == "":
        return 1
    try:
        page = int(page_token)
    except ValueError as exc:
        raise ValueError("page_token must be an integer string") from exc
    if page < 1:
        raise ValueError("page_token must be positive")
    return page


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def _positive_float(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default
    return float(value)


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _rate_limit_interval(rate_limit_per_second: float | None) -> float:
    if rate_limit_per_second is None or rate_limit_per_second <= 0:
        return 0.0
    return 1.0 / rate_limit_per_second


def _immich_asset_type_filter(media_types: set[str] | None) -> str | None:
    if not media_types or len(media_types) != 1:
        return None
    media_type = next(iter(media_types)).strip().lower()
    if media_type == "image":
        return "IMAGE"
    if media_type == "video":
        return "VIDEO"
    return None


def _search_assets_page(data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ImmichClientError(
            "Immich asset search response was not valid",
            error_code="invalid_asset_search_response",
        )
    assets_page = data.get("assets")
    if not isinstance(assets_page, Mapping):
        raise ImmichClientError(
            "Immich asset search response was not valid",
            error_code="invalid_asset_search_response",
        )
    return assets_page


def _next_page_token(value: object) -> str | None:
    if value is None or value == "":
        return None
    try:
        page = int(value)
    except (TypeError, ValueError):
        return str(value)
    if page < 1:
        return None
    return str(page)


def _asset_ref_from_response(data: Mapping[str, Any]) -> AssetRef:
    return AssetRef(
        asset_id=str(data.get("id") or ""),
        media_type=_media_type_from_response(data.get("type")),
        checksum=_optional_string(data.get("checksum")),
        metadata=_safe_asset_metadata(data),
        albums=_album_names_from_response(data),
        archived=_archived_from_response(data),
        favorite=bool(data.get("isFavorite")),
        created_at=_parse_immich_datetime(
            data.get("fileCreatedAt") or data.get("createdAt") or data.get("localDateTime")
        ),
        updated_at=_parse_immich_datetime(data.get("updatedAt")),
    )


def _metadata_from_asset_response(data: Mapping[str, Any]) -> dict[str, Any]:
    asset = _asset_ref_from_response(data)
    return {
        "asset_id": asset.asset_id,
        "media_type": asset.media_type,
        "checksum": asset.checksum,
        "metadata": dict(asset.metadata),
        "albums": list(asset.albums),
        "archived": asset.archived,
        "favorite": asset.favorite,
        "created_at": asset.created_at.isoformat() if asset.created_at else None,
        "updated_at": asset.updated_at.isoformat() if asset.updated_at else None,
    }


def _media_type_from_response(value: object) -> str:
    text = str(value or "").strip().lower()
    if text == "image":
        return "image"
    if text == "video":
        return "video"
    return text or "unknown"


def _safe_asset_metadata(data: Mapping[str, Any]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    mime_type = data.get("originalMimeType")
    if isinstance(mime_type, str) and mime_type:
        metadata["mime_type"] = mime_type
    duration = data.get("duration")
    if isinstance(duration, str) and duration:
        metadata["duration"] = duration
    visibility = data.get("visibility")
    if isinstance(visibility, str) and visibility:
        metadata["visibility"] = visibility
    return metadata


def _album_names_from_response(data: Mapping[str, Any]) -> tuple[str, ...]:
    albums = data.get("albums")
    if not isinstance(albums, list):
        return ()
    names = []
    for item in albums:
        if isinstance(item, Mapping) and isinstance(item.get("albumName"), str):
            names.append(str(item["albumName"]))
    return tuple(names)


def _archived_from_response(data: Mapping[str, Any]) -> bool:
    visibility = data.get("visibility")
    return bool(data.get("isArchived")) or visibility == "archive"


def _parse_immich_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_string(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _tag_name_matches(item: Mapping[str, Any], name: str) -> bool:
    return item.get("name") == name or item.get("value") == name


def _should_retry(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _http_error(status_code: int) -> ImmichClientError:
    if status_code in {401, 403}:
        return ImmichClientError(
            "Immich rejected the API key or permissions",
            error_code="auth_failed",
            status_code=status_code,
        )
    if status_code == 404:
        return ImmichClientError(
            "Immich resource was not found",
            error_code="not_found",
            status_code=status_code,
        )
    return ImmichClientError(
        "Immich HTTP request failed",
        error_code="request_failed",
        status_code=status_code,
    )

