from __future__ import annotations

import copy
from datetime import datetime, timezone

from mediarefinery.config import load_config, validate_config_data
from mediarefinery.immich import AssetRef, MockImmichClient
from mediarefinery.scanner import AssetScanner


class RecordingState:
    def __init__(self, skipped_ids: set[str] | None = None):
        self.skipped_ids = skipped_ids or set()
        self.calls: list[tuple[str, bool]] = []

    def needs_processing(self, asset: AssetRef, *, reprocess: bool) -> bool:
        self.calls.append((asset.asset_id, reprocess))
        return asset.asset_id not in self.skipped_ids


def _config_with_scanner(**scanner_overrides: object):
    data = copy.deepcopy(load_config("templates/config.example.yml").raw)
    data["integration"]["immich"]["page_size"] = 2
    data["scanner"].update(scanner_overrides)
    return validate_config_data(data)


def _dt(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=timezone.utc)


def _ids(scanner: AssetScanner) -> list[str]:
    return [asset.asset_id for asset in scanner.iter_candidates()]


def test_scanner_filters_media_types_across_pages() -> None:
    config = _config_with_scanner(media_types=["video"])
    client = MockImmichClient(
        [
            AssetRef("image-1", "image"),
            AssetRef("video-1", "video"),
            AssetRef("image-2", "image"),
            AssetRef("video-2", "video"),
        ]
    )

    scanner = AssetScanner(config, client)

    assert _ids(scanner) == ["video-1", "video-2"]
    assert [request["page_token"] for request in client.list_requests] == [
        None,
        "2",
    ]


def test_scanner_honors_album_include_and_exclude_filters() -> None:
    config = _config_with_scanner(
        include_albums=["family"],
        exclude_albums=["skip"],
    )
    client = MockImmichClient(
        [
            AssetRef("included", "image", albums=("family",)),
            AssetRef("excluded-wins", "image", albums=("family", "skip")),
            AssetRef("not-included", "image", albums=("work",)),
        ]
    )

    scanner = AssetScanner(config, client)

    assert _ids(scanner) == ["included"]


def test_scanner_album_mode_uses_include_albums() -> None:
    config = _config_with_scanner(mode="album", include_albums=["target"])
    client = MockImmichClient(
        [
            AssetRef("target-asset", "image", albums=("target",)),
            AssetRef("other-asset", "image", albums=("other",)),
        ]
    )

    scanner = AssetScanner(config, client)

    assert _ids(scanner) == ["target-asset"]


def test_scanner_skips_archived_assets_unless_included() -> None:
    assets = [
        AssetRef("active", "image"),
        AssetRef("archived", "image", archived=True),
    ]

    default_scanner = AssetScanner(
        _config_with_scanner(),
        MockImmichClient(assets),
    )
    include_scanner = AssetScanner(
        _config_with_scanner(include_archived=True),
        MockImmichClient(assets),
    )

    assert _ids(default_scanner) == ["active"]
    assert _ids(include_scanner) == ["active", "archived"]


def test_scanner_can_exclude_favorites() -> None:
    assets = [
        AssetRef("favorite", "image", favorite=True),
        AssetRef("regular", "image", favorite=False),
    ]

    include_scanner = AssetScanner(
        _config_with_scanner(include_favorites=True),
        MockImmichClient(assets),
    )
    exclude_scanner = AssetScanner(
        _config_with_scanner(include_favorites=False),
        MockImmichClient(assets),
    )

    assert _ids(include_scanner) == ["favorite", "regular"]
    assert _ids(exclude_scanner) == ["regular"]


def test_scanner_applies_since_filter_to_asset_timestamps() -> None:
    config = _config_with_scanner(since="2026-01-03T00:00:00Z")
    client = MockImmichClient(
        [
            AssetRef("old", "image", updated_at=_dt(2)),
            AssetRef("boundary", "image", updated_at=_dt(3)),
            AssetRef("new", "image", updated_at=_dt(4)),
            AssetRef("missing-time", "image"),
        ]
    )

    scanner = AssetScanner(config, client)

    assert _ids(scanner) == ["boundary", "new"]


def test_scanner_date_range_mode_uses_since_filter() -> None:
    config = _config_with_scanner(
        mode="date_range",
        since="2026-01-03T00:00:00Z",
    )
    client = MockImmichClient(
        [
            AssetRef("old", "image", updated_at=_dt(2)),
            AssetRef("new", "image", updated_at=_dt(4)),
        ]
    )

    scanner = AssetScanner(config, client)

    assert _ids(scanner) == ["new"]


def test_scanner_passes_reprocess_flag_to_state_hook() -> None:
    config = _config_with_scanner(reprocess=True)
    client = MockImmichClient(
        [
            AssetRef("keep", "image"),
            AssetRef("skip", "image"),
            AssetRef("filtered-video", "video"),
        ]
    )
    state = RecordingState(skipped_ids={"skip"})

    scanner = AssetScanner(config, client, state=state)

    assert _ids(scanner) == ["keep"]
    assert state.calls == [("keep", True), ("skip", True)]
