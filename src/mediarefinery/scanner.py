from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterator, Protocol

from .config import AppConfig
from .immich import AssetRef, ImmichClient


class ProcessingState(Protocol):
    def needs_processing(self, asset: AssetRef, *, reprocess: bool) -> bool:
        ...


class NoopProcessingState:
    def needs_processing(self, asset: AssetRef, *, reprocess: bool) -> bool:
        return True


@dataclass(frozen=True)
class ScannerFilters:
    mode: str
    since: datetime | None
    include_albums: frozenset[str]
    exclude_albums: frozenset[str]
    include_archived: bool
    include_favorites: bool
    media_types: frozenset[str]
    reprocess: bool

    @classmethod
    def from_config(cls, config: AppConfig) -> "ScannerFilters":
        scanner = config.scanner
        return cls(
            mode=str(scanner.get("mode") or "incremental"),
            since=parse_scanner_datetime(scanner.get("since")),
            include_albums=frozenset(scanner.get("include_albums") or ()),
            exclude_albums=frozenset(scanner.get("exclude_albums") or ()),
            include_archived=bool(scanner.get("include_archived", False)),
            include_favorites=bool(scanner.get("include_favorites", True)),
            media_types=frozenset(scanner.get("media_types") or ("image",)),
            reprocess=bool(scanner.get("reprocess", False)),
        )


class AssetScanner:
    def __init__(
        self,
        config: AppConfig,
        client: ImmichClient,
        state: ProcessingState | None = None,
    ):
        self._client = client
        self._state = state or NoopProcessingState()
        self._filters = ScannerFilters.from_config(config)
        self._skipped_count = 0
        self._page_size = int(
            ((config.raw.get("integration") or {}).get("immich") or {}).get(
                "page_size", 100
            )
        )

    @property
    def skipped_count(self) -> int:
        return self._skipped_count

    def iter_candidates(self) -> Iterator[AssetRef]:
        self._skipped_count = 0
        page_token: str | None = None
        while True:
            assets, page_token = self._client.list_assets(
                page_token=page_token,
                page_size=self._page_size,
            )
            for asset in assets:
                if not self._matches_filters(asset):
                    continue
                if self._state.needs_processing(
                    asset,
                    reprocess=self._filters.reprocess,
                ):
                    yield asset
                else:
                    self._skipped_count += 1
            if page_token is None:
                break

    def _matches_filters(self, asset: AssetRef) -> bool:
        if asset.media_type not in self._filters.media_types:
            return False
        if asset.archived and not self._filters.include_archived:
            return False
        if asset.favorite and not self._filters.include_favorites:
            return False

        asset_albums = set(asset.albums)
        if self._filters.include_albums and asset_albums.isdisjoint(
            self._filters.include_albums
        ):
            return False
        if self._filters.exclude_albums and not asset_albums.isdisjoint(
            self._filters.exclude_albums
        ):
            return False

        if self._filters.since is not None:
            asset_time = _asset_timestamp(asset)
            if asset_time is None or asset_time < self._filters.since:
                return False

        return True


def parse_scanner_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if isinstance(value, date):
        return _normalize_datetime(datetime.combine(value, datetime.min.time()))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return _normalize_datetime(datetime.fromisoformat(text.replace("Z", "+00:00")))
    raise TypeError("scanner date filters must be ISO8601 strings, dates, or null")


def _asset_timestamp(asset: AssetRef) -> datetime | None:
    timestamp = asset.updated_at or asset.created_at
    if timestamp is None:
        return None
    return _normalize_datetime(timestamp)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
