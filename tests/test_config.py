from __future__ import annotations

import copy

import pytest

from mediarefinery.config import (
    ALLOWED_ACTIONS,
    CONFIG_SCHEMA_VERSION,
    DESTRUCTIVE_ACTIONS,
    ConfigError,
    load_config,
    validate_config_data,
)


def _example_config() -> dict:
    return copy.deepcopy(load_config("templates/config.example.yml").raw)


def test_generic_example_config_is_valid() -> None:
    config = load_config("templates/config.example.yml")

    assert config.raw["version"] == CONFIG_SCHEMA_VERSION
    assert config.active_profile_name == "default"
    assert "needs_review" in config.category_ids


def test_config_version_one_is_the_v1_compatibility_contract() -> None:
    data = _example_config()

    config = validate_config_data(data)

    assert config.raw["version"] == 1


@pytest.mark.parametrize("bad_version", [None, "1", True, 2])
def test_config_version_must_be_supported_v1_schema(bad_version: object) -> None:
    data = _example_config()
    if bad_version is None:
        del data["version"]
    else:
        data["version"] = bad_version

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    errors = "\n".join(exc_info.value.errors)
    assert "version:" in errors
    assert "v1 config schema" in errors or "version 1" in errors


def test_sensitive_preset_example_config_is_valid() -> None:
    config = load_config("templates/config.preset.sensitive.example.yml")

    assert config.raw["preset"] == "sensitive-content-review"
    assert "explicit" in config.category_ids


def test_custom_non_sensitive_taxonomy_fixture_is_valid() -> None:
    config = load_config("tests/fixtures/config.custom-taxonomy.yml")

    assert config.active_profile_name == "document_sorter"
    assert {"receipt", "screenshot", "document", "other"} == config.category_ids
    assert config.policies["document"]["image"]["on_match"] == ["archive"]


def test_validation_collects_mapping_duplicate_and_url_errors() -> None:
    data = _example_config()
    data["categories"].append({"id": "ok"})
    data["classifier_profiles"]["default"]["output_mapping"]["orphan"] = "missing"
    data["integration"]["immich"]["url"] = "not-a-url"

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    errors = "\n".join(exc_info.value.errors)
    assert "duplicate category id 'ok'" in errors
    assert "unknown category id 'missing'" in errors
    assert "integration.immich.url" in errors


def test_category_id_format_is_rejected() -> None:
    data = _example_config()
    data["categories"][0]["id"] = "Needs Review"

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    assert "categories[0].id: must match" in "\n".join(exc_info.value.errors)


def test_classifier_profile_selection_must_exist() -> None:
    data = _example_config()
    data["classifier"]["profile"] = "missing_profile"

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    assert "classifier.profile: unknown profile 'missing_profile'" in "\n".join(
        exc_info.value.errors
    )


def test_unknown_policy_category_is_rejected() -> None:
    data = _example_config()
    data["policies"]["not_a_category"] = {"image": {"on_match": ["no_action"]}}

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    assert "policies.not_a_category: unknown category id 'not_a_category'" in "\n".join(
        exc_info.value.errors
    )


def test_unknown_policy_action_is_rejected() -> None:
    data = _example_config()
    data["policies"]["needs_review"]["image"]["on_match"] = ["email_operator"]

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    assert "unknown action 'email_operator'" in "\n".join(exc_info.value.errors)


def test_supported_action_registry_excludes_destructive_actions() -> None:
    assert ALLOWED_ACTIONS.isdisjoint(DESTRUCTIVE_ACTIONS)
    assert "delete" not in ALLOWED_ACTIONS
    assert "trash" not in ALLOWED_ACTIONS


def test_policy_media_type_must_be_supported() -> None:
    data = _example_config()
    data["policies"]["needs_review"]["audio"] = {"on_match": ["manual_review"]}

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    assert "policies.needs_review.audio: media type must be one of image, video" in (
        "\n".join(exc_info.value.errors)
    )


def test_scanner_filter_shape_is_validated() -> None:
    data = _example_config()
    data["scanner"]["mode"] = "everything"
    data["scanner"]["since"] = "not-a-date"
    data["scanner"]["include_albums"] = ["family", ""]
    data["scanner"]["exclude_albums"] = "archive"
    data["scanner"]["include_archived"] = "false"

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    errors = "\n".join(exc_info.value.errors)
    assert "scanner.mode: must be one of" in errors
    assert "scanner.since: must be an ISO8601 string or null" in errors
    assert "scanner.include_albums[1]: must be a non-empty string" in errors
    assert "scanner.exclude_albums: must be a list" in errors
    assert "scanner.include_archived: must be true or false" in errors


def test_scanner_mode_specific_requirements_are_validated() -> None:
    data = _example_config()
    data["scanner"]["mode"] = "album"

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    assert "scanner.include_albums: album mode requires at least one album" in (
        "\n".join(exc_info.value.errors)
    )

    data = _example_config()
    data["scanner"]["mode"] = "date_range"
    data["scanner"]["since"] = None

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    assert "scanner.since: date_range mode requires since" in "\n".join(
        exc_info.value.errors
    )


@pytest.mark.parametrize(
    "action",
    ["delete", "trash", "remove", "move-to-trash", "DELETE"],
)
def test_destructive_actions_are_rejected(action: str) -> None:
    data = _example_config()
    data["policies"]["needs_review"]["image"]["on_match"] = [action]

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    assert f"destructive action '{action}' is not supported" in "\n".join(
        exc_info.value.errors
    )


def test_archive_requires_explicit_enablement() -> None:
    data = _example_config()
    data["policies"]["needs_review"]["image"]["on_match"] = ["archive"]

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    assert "archive requires actions.archive_enabled=true" in "\n".join(
        exc_info.value.errors
    )


def test_archive_is_valid_when_explicitly_enabled() -> None:
    data = _example_config()
    data["actions"]["archive_enabled"] = True
    data["policies"]["needs_review"]["image"]["on_match"] = ["archive"]

    config = validate_config_data(data)

    assert config.policies["needs_review"]["image"]["on_match"] == ["archive"]


def test_archive_enabled_must_be_boolean() -> None:
    data = _example_config()
    data["actions"]["archive_enabled"] = "true"
    data["policies"]["needs_review"]["image"]["on_match"] = ["archive"]

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    errors = "\n".join(exc_info.value.errors)
    assert "actions.archive_enabled: must be true or false" in errors
    assert "archive requires actions.archive_enabled=true" in errors


def test_never_delete_cannot_be_disabled() -> None:
    data = _example_config()
    data["actions"]["never_delete"] = False

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    assert "actions.never_delete: must be true" in "\n".join(exc_info.value.errors)


def test_video_config_shape_is_validated() -> None:
    data = _example_config()
    data["video"] = {
        "enabled": "yes",
        "frame_count": 0,
        "frame_strategy": "scene",
        "max_duration_seconds": False,
        "ffmpeg_path": "",
    }

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    errors = "\n".join(exc_info.value.errors)
    assert "video.enabled: must be true or false" in errors
    assert "video.frame_count: must be a positive integer" in errors
    assert "video.frame_strategy: must be one of uniform" in errors
    assert "video.max_duration_seconds: must be a positive integer" in errors
    assert "video.ffmpeg_path: must be a non-empty string" in errors


def test_video_mean_aggregation_is_valid_when_explicit() -> None:
    data = _example_config()
    data["classifier_profiles"]["default"]["video_aggregation"] = "mean"

    config = validate_config_data(data)

    assert config.active_profile.video_aggregation == "mean"


def test_onnx_profile_options_are_validated() -> None:
    data = _example_config()
    data["classifier_profiles"]["default"].update(
        {
            "backend": "onnx",
            "model_path": "/models/operator-provided.onnx",
            "model_version": "operator-v1",
            "input_size": 128,
            "input_mean": [0.1, 0.2, 0.3],
            "input_std": [0.9, 0.8, 0.7],
            "input_name": "pixels",
            "output_name": "scores",
        }
    )

    config = validate_config_data(data)

    profile = config.active_profile
    assert profile.backend == "onnx"
    assert profile.model_path == "/models/operator-provided.onnx"
    assert profile.model_version == "operator-v1"
    assert profile.input_size == 128
    assert profile.input_mean == (0.1, 0.2, 0.3)
    assert profile.input_std == (0.9, 0.8, 0.7)
    assert profile.input_name == "pixels"
    assert profile.output_name == "scores"


def test_onnx_profile_rejects_bad_preprocessing_options() -> None:
    data = _example_config()
    data["classifier_profiles"]["default"].update(
        {
            "backend": "onnx",
            "model_path": "",
            "model_version": "",
            "input_size": 0,
            "input_mean": [0.1, "bad", 0.3],
            "input_std": [1.0, 0.0, 1.0],
            "input_name": "",
            "output_name": "",
        }
    )

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    errors = "\n".join(exc_info.value.errors)
    assert "model_path: onnx backend requires" in errors
    assert "model_version: must be a non-empty string" in errors
    assert "input_size: must be a positive integer" in errors
    assert "input_mean[1]: must be a number" in errors
    assert "input_std[1]: must be greater than zero" in errors
    assert "input_name: must be a non-empty string" in errors
    assert "output_name: must be a non-empty string" in errors
