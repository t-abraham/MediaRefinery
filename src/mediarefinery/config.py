from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import os
import re

import yaml


CATEGORY_ID_RE = re.compile(r"^[a-z0-9_-]+$")
CONFIG_SCHEMA_VERSION = 1
ALLOWED_MEDIA_TYPES = {"image", "video"}
ALLOWED_SCANNER_MODES = {"full", "incremental", "date_range", "album"}
ALLOWED_VIDEO_FRAME_STRATEGIES = {"uniform"}
ALLOWED_VIDEO_AGGREGATIONS = {"max", "mean"}
ALLOWED_ACTIONS = {
    "no_action",
    "add_to_review_album",
    "add_tag",
    "archive",
    "move_to_locked_folder",
    "manual_review",
}
DESTRUCTIVE_ACTIONS = {
    "delete",
    "delete_asset",
    "delete_permanently",
    "destroy",
    "destroy_asset",
    "drop",
    "erase",
    "expunge",
    "hard_delete",
    "move_to_deleted",
    "move_to_trash",
    "permanent_delete",
    "purge",
    "purge_asset",
    "remove",
    "remove_asset",
    "trash",
    "trash_asset",
    "wipe",
}
ALLOWED_TOP_LEVEL_KEYS = {
    "version",
    "preset",
    "categories",
    "classifier_profiles",
    "integration",
    "scanner",
    "classifier",
    "video",
    "actions",
    "state",
    "runtime",
    "reports",
    "policies",
}


@dataclass(frozen=True)
class Category:
    id: str
    description: str | None = None


@dataclass(frozen=True)
class ClassifierProfile:
    name: str
    backend: str
    model_path: str | None
    output_mapping: dict[str, str]
    video_aggregation: str | None = None
    model_version: str | None = None
    input_size: int = 224
    input_mean: tuple[float, float, float] = (0.0, 0.0, 0.0)
    input_std: tuple[float, float, float] = (1.0, 1.0, 1.0)
    input_name: str | None = None
    output_name: str | None = None


@dataclass(frozen=True)
class AppConfig:
    source: Path | None
    raw: dict[str, Any]
    categories: tuple[Category, ...]
    classifier_profiles: dict[str, ClassifierProfile]
    active_profile_name: str

    @property
    def category_ids(self) -> set[str]:
        return {category.id for category in self.categories}

    @property
    def active_profile(self) -> ClassifierProfile:
        return self.classifier_profiles[self.active_profile_name]

    @property
    def actions(self) -> dict[str, Any]:
        return dict(self.raw.get("actions") or {})

    @property
    def policies(self) -> dict[str, Any]:
        return dict(self.raw.get("policies") or {})

    @property
    def scanner(self) -> dict[str, Any]:
        return dict(self.raw.get("scanner") or {})

    @property
    def state(self) -> dict[str, Any]:
        return dict(self.raw.get("state") or {})

    @property
    def video(self) -> dict[str, Any]:
        return dict(self.raw.get("video") or {})

    @property
    def runtime(self) -> dict[str, Any]:
        return dict(self.raw.get("runtime") or {})

    @property
    def reports(self) -> dict[str, Any]:
        return dict(self.raw.get("reports") or {})


class ConfigError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def discover_config_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path)

    env_path = os.getenv("MEDIAREFINERY_CONFIG")
    if env_path:
        return Path(env_path)

    repo_example = Path.cwd() / "templates" / "config.example.yml"
    if repo_example.exists():
        return repo_example

    return Path("config.yml")


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    config_path = discover_config_path(path)
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"config: unable to read {config_path}: {exc}"]) from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError([f"config: invalid YAML: {exc}"]) from exc

    return validate_config_data(data, source=config_path)


def validate_config_data(data: Any, source: Path | None = None) -> AppConfig:
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ConfigError(["config: expected a YAML mapping at the document root"])

    unknown_keys = sorted(set(data) - ALLOWED_TOP_LEVEL_KEYS)
    for key in unknown_keys:
        errors.append(f"{key}: unknown top-level key")

    _validate_config_version(data.get("version"), errors)
    categories = _validate_categories(data.get("categories"), errors)
    category_ids = {category.id for category in categories}

    profiles = _validate_classifier_profiles(
        data.get("classifier_profiles"), category_ids, errors
    )
    active_profile_name = _validate_classifier_selection(
        data.get("classifier"), profiles, errors
    )

    _validate_immich(data.get("integration"), errors)
    _validate_scanner(data.get("scanner"), errors)
    _validate_video(data.get("video"), errors)
    archive_enabled = _validate_actions(data.get("actions"), errors)
    _validate_policies(data.get("policies"), category_ids, archive_enabled, errors)

    preset = data.get("preset")
    if preset is not None and (not isinstance(preset, str) or not preset.strip()):
        errors.append("preset: optional preset metadata must be a non-empty string")

    if errors:
        raise ConfigError(errors)

    return AppConfig(
        source=source,
        raw=data,
        categories=tuple(categories),
        classifier_profiles=profiles,
        active_profile_name=active_profile_name,
    )


def _validate_config_version(value: Any, errors: list[str]) -> None:
    if value is None:
        errors.append(
            f"version: required and must be integer {CONFIG_SCHEMA_VERSION} "
            "for the v1 config schema"
        )
        return
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(
            f"version: must be integer {CONFIG_SCHEMA_VERSION} "
            "for the v1 config schema"
        )
        return
    if value != CONFIG_SCHEMA_VERSION:
        errors.append(
            "version: unsupported config schema version "
            f"{value}; this MediaRefinery release supports only "
            f"version {CONFIG_SCHEMA_VERSION}"
        )


def _validate_categories(data: Any, errors: list[str]) -> list[Category]:
    categories: list[Category] = []
    if not isinstance(data, list) or not data:
        errors.append("categories: must be a non-empty list")
        return categories

    seen: set[str] = set()
    for index, item in enumerate(data):
        path = f"categories[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: must be a mapping")
            continue

        category_id = item.get("id")
        if not isinstance(category_id, str) or not category_id:
            errors.append(f"{path}.id: must be a non-empty string")
            continue
        if not CATEGORY_ID_RE.fullmatch(category_id):
            errors.append(
                f"{path}.id: must match {CATEGORY_ID_RE.pattern}"
            )
        if category_id in seen:
            errors.append(f"{path}.id: duplicate category id '{category_id}'")
        seen.add(category_id)

        description = item.get("description")
        if description is not None and not isinstance(description, str):
            errors.append(f"{path}.description: must be a string when present")
        categories.append(Category(id=category_id, description=description))

    return categories


def _validate_classifier_profiles(
    data: Any, category_ids: set[str], errors: list[str]
) -> dict[str, ClassifierProfile]:
    profiles: dict[str, ClassifierProfile] = {}
    if not isinstance(data, dict) or not data:
        errors.append("classifier_profiles: must be a non-empty mapping")
        return profiles

    for name, profile in data.items():
        path = f"classifier_profiles.{name}"
        if not isinstance(name, str) or not name:
            errors.append("classifier_profiles: profile names must be non-empty strings")
            continue
        if not isinstance(profile, dict):
            errors.append(f"{path}: must be a mapping")
            continue

        backend = profile.get("backend")
        if not isinstance(backend, str) or not backend:
            errors.append(f"{path}.backend: must be a non-empty string")
            backend = ""

        model_path = profile.get("model_path")
        if model_path is not None and not isinstance(model_path, str):
            errors.append(f"{path}.model_path: must be a string or null")
            model_path = None
        if (
            str(backend).strip().lower() == "onnx"
            and not _non_empty_string(model_path)
        ):
            errors.append(f"{path}.model_path: onnx backend requires a model path")

        model_version = profile.get("model_version")
        if model_version is not None and not _non_empty_string(model_version):
            errors.append(f"{path}.model_version: must be a non-empty string")
            model_version = None

        input_size = _validate_positive_int(
            profile.get("input_size"),
            f"{path}.input_size",
            errors,
            default=224,
        )
        input_mean = _validate_float_triplet(
            profile.get("input_mean"),
            f"{path}.input_mean",
            errors,
            default=(0.0, 0.0, 0.0),
            positive=False,
        )
        input_std = _validate_float_triplet(
            profile.get("input_std"),
            f"{path}.input_std",
            errors,
            default=(1.0, 1.0, 1.0),
            positive=True,
        )
        input_name = profile.get("input_name")
        if input_name is not None and not _non_empty_string(input_name):
            errors.append(f"{path}.input_name: must be a non-empty string")
            input_name = None
        output_name = profile.get("output_name")
        if output_name is not None and not _non_empty_string(output_name):
            errors.append(f"{path}.output_name: must be a non-empty string")
            output_name = None

        output_mapping = profile.get("output_mapping")
        mapping: dict[str, str] = {}
        if not isinstance(output_mapping, dict) or not output_mapping:
            errors.append(f"{path}.output_mapping: must be a non-empty mapping")
        else:
            for raw_label, category_id in output_mapping.items():
                mapping_path = f"{path}.output_mapping.{raw_label}"
                if not isinstance(raw_label, str) or not raw_label:
                    errors.append(f"{path}.output_mapping: raw labels must be strings")
                    continue
                if not isinstance(category_id, str):
                    errors.append(f"{mapping_path}: category id must be a string")
                    continue
                if category_ids and category_id not in category_ids:
                    errors.append(
                        f"{mapping_path}: unknown category id '{category_id}'"
                    )
                mapping[raw_label] = category_id

        video_aggregation = profile.get("video_aggregation")
        if (
            video_aggregation is not None
            and video_aggregation not in ALLOWED_VIDEO_AGGREGATIONS
        ):
            errors.append(
                f"{path}.video_aggregation: must be 'max', 'mean', or omitted"
            )

        profiles[name] = ClassifierProfile(
            name=name,
            backend=backend,
            model_path=model_path,
            output_mapping=mapping,
            video_aggregation=video_aggregation,
            model_version=model_version,
            input_size=input_size,
            input_mean=input_mean,
            input_std=input_std,
            input_name=input_name,
            output_name=output_name,
        )

    return profiles


def _validate_classifier_selection(
    data: Any, profiles: dict[str, ClassifierProfile], errors: list[str]
) -> str:
    if not isinstance(data, dict):
        errors.append("classifier: must be a mapping")
        return ""

    active_profile = data.get("profile")
    if not isinstance(active_profile, str) or not active_profile:
        errors.append("classifier.profile: must be a non-empty string")
        return ""

    if profiles and active_profile not in profiles:
        errors.append(f"classifier.profile: unknown profile '{active_profile}'")

    return active_profile


def _validate_immich(data: Any, errors: list[str]) -> None:
    if not isinstance(data, dict):
        errors.append("integration: must be a mapping")
        return

    immich = data.get("immich")
    if not isinstance(immich, dict):
        errors.append("integration.immich: must be a mapping")
        return

    url = immich.get("url")
    if not isinstance(url, str) or not url:
        errors.append("integration.immich.url: must be a non-empty string")
        return

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        errors.append("integration.immich.url: must be an http(s) URL")

    api_key_env = immich.get("api_key_env")
    if api_key_env is not None and not isinstance(api_key_env, str):
        errors.append("integration.immich.api_key_env: must name an env var")


def _validate_scanner(data: Any, errors: list[str]) -> None:
    if data is None:
        return
    if not isinstance(data, dict):
        errors.append("scanner: must be a mapping")
        return

    mode = data.get("mode")
    if mode is not None and mode not in ALLOWED_SCANNER_MODES:
        errors.append(
            "scanner.mode: must be one of album, date_range, full, incremental"
        )

    since = data.get("since")
    if since is not None:
        if isinstance(since, date):
            pass
        elif not isinstance(since, str) or not since.strip():
            errors.append("scanner.since: must be an ISO8601 string or null")
        else:
            try:
                datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
            except ValueError:
                errors.append("scanner.since: must be an ISO8601 string or null")

    _validate_string_list(data, "include_albums", "scanner.include_albums", errors)
    _validate_string_list(data, "exclude_albums", "scanner.exclude_albums", errors)

    include_albums = data.get("include_albums") or []
    if mode == "album" and not include_albums:
        errors.append("scanner.include_albums: album mode requires at least one album")
    if mode == "date_range" and since is None:
        errors.append("scanner.since: date_range mode requires since")

    for key in ("include_archived", "include_favorites", "reprocess"):
        if key in data and not isinstance(data[key], bool):
            errors.append(f"scanner.{key}: must be true or false")

    media_types = data.get("media_types")
    if media_types is None:
        return
    if not isinstance(media_types, list) or not media_types:
        errors.append("scanner.media_types: must be a non-empty list")
        return

    for index, media_type in enumerate(media_types):
        if media_type not in ALLOWED_MEDIA_TYPES:
            errors.append(
                f"scanner.media_types[{index}]: must be one of image, video"
            )


def _validate_video(data: Any, errors: list[str]) -> None:
    if data is None:
        return
    if not isinstance(data, dict):
        errors.append("video: must be a mapping")
        return

    enabled = data.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        errors.append("video.enabled: must be true or false")

    frame_count = data.get("frame_count")
    if frame_count is not None and (
        not isinstance(frame_count, int)
        or isinstance(frame_count, bool)
        or frame_count < 1
    ):
        errors.append("video.frame_count: must be a positive integer")

    frame_strategy = data.get("frame_strategy")
    if (
        frame_strategy is not None
        and frame_strategy not in ALLOWED_VIDEO_FRAME_STRATEGIES
    ):
        errors.append("video.frame_strategy: must be one of uniform")

    max_duration_seconds = data.get("max_duration_seconds")
    if max_duration_seconds is not None and (
        not isinstance(max_duration_seconds, int)
        or isinstance(max_duration_seconds, bool)
        or max_duration_seconds < 1
    ):
        errors.append("video.max_duration_seconds: must be a positive integer")

    ffmpeg_path = data.get("ffmpeg_path")
    if ffmpeg_path is not None and (
        not isinstance(ffmpeg_path, str) or not ffmpeg_path.strip()
    ):
        errors.append("video.ffmpeg_path: must be a non-empty string")


def _validate_string_list(
    data: dict[str, Any],
    key: str,
    path: str,
    errors: list[str],
) -> None:
    values = data.get(key)
    if values is None:
        return
    if not isinstance(values, list):
        errors.append(f"{path}: must be a list")
        return
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{path}[{index}]: must be a non-empty string")


def _validate_positive_int(
    value: Any,
    path: str,
    errors: list[str],
    *,
    default: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        errors.append(f"{path}: must be a positive integer")
        return default
    return value


def _validate_float_triplet(
    value: Any,
    path: str,
    errors: list[str],
    *,
    default: tuple[float, float, float],
    positive: bool,
) -> tuple[float, float, float]:
    if value is None:
        return default
    if not isinstance(value, list) or len(value) != 3:
        errors.append(f"{path}: must be a list of three numbers")
        return default

    numbers: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            errors.append(f"{path}[{index}]: must be a number")
            return default
        number = float(item)
        if positive and number <= 0:
            errors.append(f"{path}[{index}]: must be greater than zero")
            return default
        numbers.append(number)

    return (numbers[0], numbers[1], numbers[2])


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_actions(data: Any, errors: list[str]) -> bool:
    if data is None:
        return False
    if not isinstance(data, dict):
        errors.append("actions: must be a mapping")
        return False

    archive_enabled = data.get("archive_enabled", False)
    if "archive_enabled" in data and not isinstance(archive_enabled, bool):
        errors.append("actions.archive_enabled: must be true or false")
        archive_enabled = False

    never_delete = data.get("never_delete", True)
    if "never_delete" in data and never_delete is not True:
        errors.append(
            "actions.never_delete: must be true; delete/trash is not a product feature"
        )

    return archive_enabled is True


def _validate_policies(
    data: Any, category_ids: set[str], archive_enabled: bool, errors: list[str]
) -> None:
    if not isinstance(data, dict):
        errors.append("policies: must be a mapping")
        return

    for category_id, media_policy in data.items():
        category_path = f"policies.{category_id}"
        if not isinstance(category_id, str) or not category_id:
            errors.append("policies: category keys must be non-empty strings")
            continue
        if category_ids and category_id not in category_ids:
            errors.append(f"{category_path}: unknown category id '{category_id}'")
        if not isinstance(media_policy, dict):
            errors.append(f"{category_path}: must be a mapping")
            continue

        for media_type, rule in media_policy.items():
            rule_path = f"{category_path}.{media_type}"
            if not isinstance(media_type, str) or media_type not in ALLOWED_MEDIA_TYPES:
                errors.append(f"{rule_path}: media type must be one of image, video")
                continue
            if not isinstance(rule, dict):
                errors.append(f"{rule_path}: must be a mapping")
                continue

            on_match = rule.get("on_match")
            if not isinstance(on_match, list):
                errors.append(f"{rule_path}.on_match: must be a list")
                continue
            if not on_match:
                errors.append(f"{rule_path}.on_match: must include at least one action")
                continue

            for index, action in enumerate(on_match):
                action_path = f"{rule_path}.on_match[{index}]"
                if not isinstance(action, str) or not action:
                    errors.append(f"{action_path}: must be a non-empty string")
                    continue
                normalized_action = action.strip().lower().replace("-", "_")
                if normalized_action in DESTRUCTIVE_ACTIONS:
                    errors.append(
                        f"{action_path}: destructive action '{action}' is not supported"
                    )
                    continue
                if action not in ALLOWED_ACTIONS:
                    errors.append(f"{action_path}: unknown action '{action}'")
                    continue
                if action == "archive" and not archive_enabled:
                    errors.append(
                        f"{action_path}: archive requires actions.archive_enabled=true"
                    )
                if action == "archive":
                    import warnings as _warnings

                    _warnings.warn(
                        f"{action_path}: 'archive' action is deprecated in v2; "
                        "use 'move_to_locked_folder' instead",
                        DeprecationWarning,
                        stacklevel=2,
                    )

