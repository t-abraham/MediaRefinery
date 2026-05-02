from __future__ import annotations

from pathlib import Path

import pytest

from mediarefinery.classifier import (
    ClassifierBackendError,
    ClassifierError,
    ClassifierInput,
    ClassifierMappingError,
    ConfiguredClassifier,
    RawModelOutput,
    available_classifier_backends,
    create_classifier,
)
from mediarefinery.config import ConfigError, validate_config_data
from mediarefinery.onnx_backend import OnnxClassifierBackend


def _config_data() -> dict:
    return {
        "version": 1,
        "categories": [
            {"id": "invoice"},
            {"id": "project_review"},
            {"id": "personal_keep"},
        ],
        "classifier_profiles": {
            "finance": {
                "backend": "noop",
                "model_path": None,
                "output_mapping": {"raw_shared": "invoice"},
            },
            "personal": {
                "backend": "noop",
                "model_path": None,
                "output_mapping": {"raw_shared": "personal_keep"},
            },
        },
        "integration": {
            "immich": {
                "url": "https://immich.example.local",
                "api_key_env": "IMMICH_API_KEY",
            }
        },
        "scanner": {"media_types": ["image"]},
        "classifier": {"profile": "finance"},
        "actions": {"dry_run": True, "archive_enabled": False},
        "policies": {
            "invoice": {"image": {"on_match": ["add_tag"]}},
            "project_review": {"image": {"on_match": ["add_to_review_album"]}},
            "personal_keep": {"image": {"on_match": ["no_action"]}},
        },
    }


def test_same_raw_label_maps_to_different_user_categories_by_profile() -> None:
    data = _config_data()
    classifier_input = ClassifierInput(
        asset_id="synthetic-asset",
        media_type="image",
        metadata={"mock_raw_label": "raw_shared"},
    )

    finance_config = validate_config_data(data)
    finance_result = create_classifier(finance_config).predict_one(classifier_input)

    data["classifier"]["profile"] = "personal"
    personal_config = validate_config_data(data)
    personal_result = create_classifier(personal_config).predict_one(classifier_input)

    assert finance_result.raw_label == "raw_shared"
    assert personal_result.raw_label == "raw_shared"
    assert finance_result.raw_scores == {"raw_shared": 1.0}
    assert personal_result.raw_scores == {"raw_shared": 1.0}
    assert finance_result.category_id == "invoice"
    assert personal_result.category_id == "personal_keep"


def test_noop_backend_is_deterministic_without_model_files() -> None:
    config = validate_config_data(_config_data())
    classifier = create_classifier(config)
    classifier_input = ClassifierInput("synthetic-asset", "image")

    first = classifier.predict_one(classifier_input)
    second = classifier.predict_one(classifier_input)

    assert config.active_profile.model_path is None
    assert first == second
    assert first.raw_label == "raw_shared"
    assert first.category_id == "invoice"


def test_unmapped_backend_output_fails_with_profile_path() -> None:
    config = validate_config_data(_config_data())
    classifier = create_classifier(config)
    classifier_input = ClassifierInput(
        "synthetic-asset",
        "image",
        metadata={"mock_raw_label": "raw_missing"},
    )

    with pytest.raises(ClassifierMappingError) as exc_info:
        classifier.predict_one(classifier_input)

    message = str(exc_info.value)
    assert "raw_missing" in message
    assert "classifier_profiles.finance.output_mapping" in message
    assert "raw_shared" in message


def test_unregistered_backend_fails_with_available_backends() -> None:
    data = _config_data()
    data["classifier_profiles"]["finance"]["backend"] = "missing_backend"
    config = validate_config_data(data)

    with pytest.raises(ClassifierBackendError) as exc_info:
        create_classifier(config)

    message = str(exc_info.value)
    assert "unsupported backend 'missing_backend'" in message
    assert "noop" in message
    assert "onnx" in message


def test_onnx_backend_is_registered() -> None:
    assert "noop" in available_classifier_backends()
    assert "onnx" in available_classifier_backends()


def test_onnx_backend_maps_inference_scores_through_profile(
    tmp_path,
    monkeypatch,
) -> None:
    model_path = tmp_path / "classifier.onnx"
    model_path.write_bytes(b"synthetic placeholder")
    data = _config_data()
    data["classifier_profiles"]["finance"].update(
        {
            "backend": "onnx",
            "model_path": str(model_path),
            "model_version": "synthetic-v1",
            "output_mapping": {
                "raw_receipt": "invoice",
                "raw_review": "project_review",
            },
        }
    )
    config = validate_config_data(data)
    fake_session = _FakeOnnxSession([[0.15, 0.85]])
    monkeypatch.setattr(
        "mediarefinery.onnx_backend._load_onnx_dependencies",
        lambda: _FakeOnnxDependencies(fake_session),
    )
    monkeypatch.setattr(
        OnnxClassifierBackend,
        "_preprocess_input",
        lambda self, classifier_input: _FakeTensor([1.0]),
    )

    classifier = create_classifier(config)
    result = classifier.predict_one(
        ClassifierInput(
            asset_id="synthetic-asset",
            media_type="image",
            data=b"synthetic image bytes",
        )
    )

    assert classifier.backend.version == "synthetic-v1"
    assert fake_session.feed_names == ["image"]
    assert result.asset_id == "synthetic-asset"
    assert result.raw_label == "raw_review"
    assert result.raw_scores == {"raw_receipt": 0.15, "raw_review": 0.85}
    assert result.category_id == "project_review"


def test_onnx_backend_honors_configured_io_names(tmp_path, monkeypatch) -> None:
    model_path = tmp_path / "classifier.onnx"
    model_path.write_bytes(b"synthetic placeholder")
    data = _config_data()
    data["classifier_profiles"]["finance"].update(
        {
            "backend": "onnx",
            "model_path": str(model_path),
            "input_name": "pixels",
            "output_name": "probabilities",
            "output_mapping": {"raw_receipt": "invoice"},
        }
    )
    config = validate_config_data(data)
    fake_session = _FakeOnnxSession(
        [[1.0]],
        input_names=("unused", "pixels"),
        output_names=("unused", "probabilities"),
    )
    monkeypatch.setattr(
        "mediarefinery.onnx_backend._load_onnx_dependencies",
        lambda: _FakeOnnxDependencies(fake_session),
    )
    monkeypatch.setattr(
        OnnxClassifierBackend,
        "_preprocess_input",
        lambda self, classifier_input: _FakeTensor([1.0]),
    )

    classifier = create_classifier(config)
    result = classifier.predict_one(
        ClassifierInput("synthetic-asset", "image", data=b"synthetic")
    )

    assert fake_session.output_names == [["probabilities"]]
    assert fake_session.feed_names == ["pixels"]
    assert result.category_id == "invoice"


def test_onnx_model_load_failure_is_sanitized(tmp_path, monkeypatch) -> None:
    model_path = tmp_path / "private-model.onnx"
    model_path.write_bytes(b"synthetic placeholder")
    private_path = r"C:\Users\Alice\Pictures\private-model.onnx"
    data = _config_data()
    data["classifier_profiles"]["finance"].update(
        {"backend": "onnx", "model_path": str(model_path)}
    )
    config = validate_config_data(data)

    class FailingRuntime:
        @staticmethod
        def InferenceSession(model_path, providers):
            raise RuntimeError(f"load failed at {private_path}")

    monkeypatch.setattr(
        "mediarefinery.onnx_backend._load_onnx_dependencies",
        lambda: _FakeOnnxDependencies(runtime=FailingRuntime),
    )

    with pytest.raises(ClassifierBackendError) as exc_info:
        create_classifier(config)

    message = str(exc_info.value)
    assert message == "onnx model could not be loaded"
    assert "Alice" not in message
    assert "private-model.onnx" not in message


def test_onnx_missing_model_path_fails_during_config_validation() -> None:
    data = _config_data()
    data["classifier_profiles"]["finance"].update(
        {"backend": "onnx", "model_path": None}
    )

    with pytest.raises(ConfigError) as exc_info:
        validate_config_data(data)

    message = str(exc_info.value)
    assert "model_path" in message
    assert "onnx backend requires" in message


def test_onnx_unreadable_model_path_failure_is_sanitized(tmp_path) -> None:
    private_path = tmp_path / "private-model.onnx"
    data = _config_data()
    data["classifier_profiles"]["finance"].update(
        {"backend": "onnx", "model_path": str(private_path)}
    )
    config = validate_config_data(data)

    with pytest.raises(ClassifierBackendError) as exc_info:
        create_classifier(config)

    message = str(exc_info.value)
    assert message == "onnx model_path is not a readable file"
    assert str(private_path) not in message


def test_onnx_output_label_count_must_match_mapping(tmp_path, monkeypatch) -> None:
    model_path = tmp_path / "classifier.onnx"
    model_path.write_bytes(b"synthetic placeholder")
    data = _config_data()
    data["classifier_profiles"]["finance"].update(
        {
            "backend": "onnx",
            "model_path": str(model_path),
            "output_mapping": {
                "raw_receipt": "invoice",
                "raw_review": "project_review",
            },
        }
    )
    config = validate_config_data(data)
    monkeypatch.setattr(
        "mediarefinery.onnx_backend._load_onnx_dependencies",
        lambda: _FakeOnnxDependencies(_FakeOnnxSession([[1.0]])),
    )
    monkeypatch.setattr(
        OnnxClassifierBackend,
        "_preprocess_input",
        lambda self, classifier_input: _FakeTensor([1.0]),
    )
    classifier = create_classifier(config)

    with pytest.raises(ClassifierError) as exc_info:
        classifier.predict_one(
            ClassifierInput("synthetic-asset", "image", data=b"synthetic")
        )

    assert str(exc_info.value) == "onnx output label count did not match output_mapping"


def test_video_aggregation_defaults_to_max() -> None:
    data = _config_data()
    data["classifier_profiles"]["finance"]["output_mapping"] = {
        "raw_receipt": "invoice",
        "raw_review": "project_review",
    }
    config = validate_config_data(data)
    classifier = ConfiguredClassifier(
        config.active_profile,
        _SequenceBackend(
            config.active_profile,
            [
                RawModelOutput(
                    asset_id="video-1",
                    raw_label="raw_receipt",
                    raw_scores={"raw_receipt": 0.65, "raw_review": 0.15},
                ),
                RawModelOutput(
                    asset_id="video-1",
                    raw_label="raw_review",
                    raw_scores={"raw_receipt": 0.2, "raw_review": 0.8},
                ),
            ],
        ),
    )

    result = classifier.predict_aggregate(
        [
            ClassifierInput(asset_id="video-1", media_type="video"),
            ClassifierInput(asset_id="video-1", media_type="video"),
        ],
        asset_id="video-1",
    )

    assert result.category_id == "project_review"
    assert result.raw_label == "raw_review"
    assert result.raw_labels == ("raw_receipt", "raw_review")
    assert result.raw_scores == {"raw_receipt": 0.65, "raw_review": 0.8}


def test_video_aggregation_supports_tested_mean_strategy() -> None:
    data = _config_data()
    data["classifier_profiles"]["finance"]["video_aggregation"] = "mean"
    data["classifier_profiles"]["finance"]["output_mapping"] = {
        "raw_receipt": "invoice",
        "raw_review": "project_review",
    }
    config = validate_config_data(data)
    classifier = ConfiguredClassifier(
        config.active_profile,
        _SequenceBackend(
            config.active_profile,
            [
                RawModelOutput(
                    asset_id="video-1",
                    raw_label="raw_receipt",
                    raw_scores={"raw_receipt": 0.9, "raw_review": 0.4},
                ),
                RawModelOutput(
                    asset_id="video-1",
                    raw_label="raw_review",
                    raw_scores={"raw_receipt": 0.0, "raw_review": 0.6},
                ),
            ],
        ),
    )

    result = classifier.predict_aggregate(
        [
            ClassifierInput(asset_id="video-1", media_type="video"),
            ClassifierInput(asset_id="video-1", media_type="video"),
        ],
        asset_id="video-1",
    )

    assert result.category_id == "project_review"
    assert result.raw_label == "raw_review"
    assert result.raw_scores == {"raw_receipt": 0.45, "raw_review": 0.5}


def test_classifier_module_does_not_name_sensitive_preset_categories() -> None:
    source = Path("src/mediarefinery/classifier.py").read_text(encoding="utf-8").lower()

    for forbidden in ("safe", "nsfw", "explicit", "suggestive"):
        assert forbidden not in source


class _FakeTensor:
    def __init__(self, value):
        self.value = value

    def astype(self, dtype, copy=False):
        return self


class _FakeNumpy:
    float32 = "float32"

    @staticmethod
    def stack(values, axis=0):
        return _FakeTensor(list(values))


class _FakeNode:
    def __init__(self, name: str):
        self.name = name


class _FakeOnnxSession:
    def __init__(
        self,
        outputs,
        *,
        input_names=("image",),
        output_names=("scores",),
    ):
        self._outputs = outputs
        self._input_names = input_names
        self._output_names = output_names
        self.feed_names: list[str] = []
        self.output_names: list[list[str]] = []

    def get_inputs(self):
        return [_FakeNode(name) for name in self._input_names]

    def get_outputs(self):
        return [_FakeNode(name) for name in self._output_names]

    def run(self, output_names, feed):
        self.output_names.append(list(output_names))
        self.feed_names.extend(feed)
        return [self._outputs]


class _FakeOnnxRuntime:
    def __init__(self, session):
        self.session = session

    def InferenceSession(self, model_path, providers):
        return self.session


class _FakeOnnxDependencies:
    def __init__(self, session=None, *, runtime=None):
        self.ort = runtime or _FakeOnnxRuntime(session)
        self.np = _FakeNumpy()
        self.image = object()
        self.image_ops = object()


class _SequenceBackend:
    version = "test"

    def __init__(self, profile, outputs: list[RawModelOutput]):
        self.profile = profile
        self.outputs = outputs
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def predict_batch(self, inputs):
        assert self.loaded
        assert len(inputs) == len(self.outputs)
        return list(self.outputs)
