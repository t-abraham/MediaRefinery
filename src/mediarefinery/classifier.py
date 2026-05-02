from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .config import AppConfig, ClassifierProfile


DEFAULT_VIDEO_AGGREGATION = "max"
SUPPORTED_VIDEO_AGGREGATIONS = frozenset({"max", "mean"})


@dataclass(frozen=True)
class ClassifierInput:
    asset_id: str
    media_type: str
    metadata: Mapping[str, str] = field(default_factory=dict)
    data: bytes | None = field(default=None, repr=False, compare=False)
    content_type: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class RawModelOutput:
    asset_id: str
    raw_label: str
    raw_scores: dict[str, float]
    raw_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.raw_labels:
            return
        object.__setattr__(self, "raw_labels", (self.raw_label,))


@dataclass(frozen=True)
class ClassificationResult:
    asset_id: str
    category_id: str
    raw_scores: dict[str, float]
    raw_label: str | None = None
    raw_labels: tuple[str, ...] = ()


class ClassifierError(Exception):
    """Base exception for classifier setup and prediction failures."""


class ClassifierBackendError(ClassifierError):
    """Raised when config names a backend that is not registered."""


class ClassifierMappingError(ClassifierError):
    """Raised when raw backend output cannot resolve to a user category."""


@runtime_checkable
class ClassifierBackend(Protocol):
    profile: ClassifierProfile
    version: str

    def load(self) -> None:
        ...

    def predict_batch(self, inputs: Sequence[ClassifierInput]) -> list[RawModelOutput]:
        ...


class NoopClassifierBackend:
    """Deterministic backend for tests and integration smoke runs."""

    version = "noop"

    def __init__(self, profile: ClassifierProfile):
        self.profile = profile
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def predict_batch(self, inputs: Sequence[ClassifierInput]) -> list[RawModelOutput]:
        if not self.loaded:
            self.load()

        return [self._predict_one(classifier_input) for classifier_input in inputs]

    def _predict_one(self, classifier_input: ClassifierInput) -> RawModelOutput:
        raw_label = classifier_input.metadata.get("mock_raw_label")
        if raw_label is None:
            raw_label = classifier_input.metadata.get("noop_raw_label")
        if raw_label is None:
            raw_label = next(iter(self.profile.output_mapping))

        return RawModelOutput(
            asset_id=classifier_input.asset_id,
            raw_label=raw_label,
            raw_labels=(raw_label,),
            raw_scores={raw_label: 1.0},
        )


class ConfiguredClassifier:
    def __init__(self, profile: ClassifierProfile, backend: ClassifierBackend):
        self.profile = profile
        self.backend = backend
        self.backend.load()

    def predict_one(self, classifier_input: ClassifierInput) -> ClassificationResult:
        return self.predict_batch([classifier_input])[0]

    def predict_batch(
        self,
        inputs: Sequence[ClassifierInput],
    ) -> list[ClassificationResult]:
        input_list = list(inputs)
        raw_outputs = self._predict_raw_batch(input_list)
        return [
            resolve_model_output(self.profile, raw_output)
            for raw_output in raw_outputs
        ]

    def predict_aggregate(
        self,
        inputs: Sequence[ClassifierInput],
        *,
        asset_id: str,
        aggregation: str | None = None,
    ) -> ClassificationResult:
        input_list = list(inputs)
        if not input_list:
            raise ClassifierError("video classification requires at least one frame")
        raw_outputs = self._predict_raw_batch(input_list)
        return aggregate_model_outputs(
            self.profile,
            raw_outputs,
            asset_id=asset_id,
            aggregation=aggregation,
        )

    def _predict_raw_batch(
        self,
        inputs: Sequence[ClassifierInput],
    ) -> list[RawModelOutput]:
        input_list = list(inputs)
        raw_outputs = list(self.backend.predict_batch(input_list))
        if len(raw_outputs) != len(input_list):
            raise ClassifierError(
                "classifier backend returned "
                f"{len(raw_outputs)} outputs for {len(input_list)} inputs"
            )

        for classifier_input, raw_output in zip(input_list, raw_outputs, strict=True):
            if raw_output.asset_id != classifier_input.asset_id:
                raise ClassifierError(
                    "classifier backend output asset_id "
                    f"'{raw_output.asset_id}' did not match input asset_id "
                    f"'{classifier_input.asset_id}'"
                )
        return raw_outputs


class NoopClassifier(ConfiguredClassifier):
    """Compatibility wrapper for the built-in noop backend."""

    def __init__(self, config: AppConfig):
        profile = config.active_profile
        super().__init__(profile, NoopClassifierBackend(profile))


BackendFactory = Callable[[ClassifierProfile], ClassifierBackend]

from .onnx_backend import OnnxClassifierBackend

_BACKEND_REGISTRY: dict[str, BackendFactory] = {
    "noop": NoopClassifierBackend,
    "onnx": OnnxClassifierBackend,
}


def available_classifier_backends() -> tuple[str, ...]:
    return tuple(sorted(_BACKEND_REGISTRY))


def register_classifier_backend(name: str, factory: BackendFactory) -> None:
    backend_name = _normalize_backend_name(name)
    if not backend_name:
        raise ValueError("classifier backend name must be a non-empty string")
    _BACKEND_REGISTRY[backend_name] = factory


def create_classifier(config: AppConfig) -> ConfiguredClassifier:
    profile = config.active_profile
    backend = create_classifier_backend(profile)
    return ConfiguredClassifier(profile, backend)


def create_classifier_backend(profile: ClassifierProfile) -> ClassifierBackend:
    backend_name = _normalize_backend_name(profile.backend)
    factory = _BACKEND_REGISTRY.get(backend_name)
    if factory is None:
        available = ", ".join(available_classifier_backends()) or "<none>"
        raise ClassifierBackendError(
            f"classifier profile '{profile.name}' uses unsupported backend "
            f"'{profile.backend}'. Registered backends: {available}"
        )
    return factory(profile)


def resolve_model_output(
    profile: ClassifierProfile,
    output: RawModelOutput,
) -> ClassificationResult:
    category_id = profile.output_mapping.get(output.raw_label)
    if category_id is None:
        configured = ", ".join(sorted(profile.output_mapping)) or "<none>"
        raise ClassifierMappingError(
            f"classifier profile '{profile.name}' could not map raw label "
            f"'{output.raw_label}' via "
            f"classifier_profiles.{profile.name}.output_mapping. "
            f"Configured raw labels: {configured}"
        )

    return ClassificationResult(
        asset_id=output.asset_id,
        category_id=category_id,
        raw_scores=dict(output.raw_scores),
        raw_label=output.raw_label,
        raw_labels=tuple(output.raw_labels),
    )


def aggregate_model_outputs(
    profile: ClassifierProfile,
    outputs: Sequence[RawModelOutput],
    *,
    asset_id: str,
    aggregation: str | None = None,
) -> ClassificationResult:
    output_list = list(outputs)
    if not output_list:
        raise ClassifierError("video classification requires at least one frame output")

    strategy = aggregation or profile.video_aggregation or DEFAULT_VIDEO_AGGREGATION
    if strategy not in SUPPORTED_VIDEO_AGGREGATIONS:
        supported = ", ".join(sorted(SUPPORTED_VIDEO_AGGREGATIONS))
        raise ClassifierError(
            f"unsupported video aggregation '{strategy}'. Supported values: {supported}"
        )

    scores_by_label: dict[str, list[float]] = {}
    frame_raw_labels: list[str] = []
    for output in output_list:
        if output.asset_id != asset_id:
            raise ClassifierError(
                "classifier backend output asset_id "
                f"'{output.asset_id}' did not match aggregate asset_id '{asset_id}'"
            )
        frame_raw_labels.append(output.raw_label)
        for raw_label, score in output.raw_scores.items():
            if raw_label in profile.output_mapping:
                scores_by_label.setdefault(raw_label, []).append(float(score))

    if not scores_by_label:
        configured = ", ".join(sorted(profile.output_mapping)) or "<none>"
        observed = ", ".join(sorted(set(frame_raw_labels))) or "<none>"
        raise ClassifierMappingError(
            f"classifier profile '{profile.name}' could not aggregate video outputs. "
            f"Configured raw labels: {configured}. Observed raw labels: {observed}"
        )

    if strategy == "mean":
        aggregated_scores = {
            raw_label: sum(scores) / len(scores)
            for raw_label, scores in scores_by_label.items()
        }
    else:
        aggregated_scores = {
            raw_label: max(scores) for raw_label, scores in scores_by_label.items()
        }

    mapping_order = {
        raw_label: index for index, raw_label in enumerate(profile.output_mapping)
    }
    best_label = max(
        aggregated_scores,
        key=lambda raw_label: (
            aggregated_scores[raw_label],
            -mapping_order.get(raw_label, len(mapping_order)),
        ),
    )
    return resolve_model_output(
        profile,
        RawModelOutput(
            asset_id=asset_id,
            raw_label=best_label,
            raw_labels=tuple(frame_raw_labels),
            raw_scores=aggregated_scores,
        ),
    )


def _normalize_backend_name(name: str) -> str:
    return name.strip().lower()

