from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from .classifier import (
    ClassifierBackendError,
    ClassifierError,
    ClassifierInput,
    RawModelOutput,
)
from .config import ClassifierProfile


@dataclass(frozen=True)
class _OnnxDependencies:
    ort: Any
    np: Any
    image: Any
    image_ops: Any


class OnnxClassifierBackend:
    version = "onnx"

    def __init__(self, profile: ClassifierProfile):
        self.profile = profile
        self.version = profile.model_version or "onnx"
        self.loaded = False
        self._deps: _OnnxDependencies | None = None
        self._session: Any | None = None
        self._input_name: str | None = None
        self._output_name: str | None = None

    def load(self) -> None:
        model_path = self._readable_model_path()
        deps = _load_onnx_dependencies()
        try:
            session = deps.ort.InferenceSession(
                str(model_path),
                providers=["CPUExecutionProvider"],
            )
            input_name = _select_io_name(
                session.get_inputs(),
                configured_name=self.profile.input_name,
                kind="input",
            )
            output_name = _select_io_name(
                session.get_outputs(),
                configured_name=self.profile.output_name,
                kind="output",
            )
        except ClassifierError:
            raise
        except Exception as exc:
            raise ClassifierBackendError(
                "onnx model could not be loaded"
            ) from exc

        self._deps = deps
        self._session = session
        self._input_name = input_name
        self._output_name = output_name
        self.loaded = True

    def predict_batch(self, inputs: Sequence[ClassifierInput]) -> list[RawModelOutput]:
        if not self.loaded:
            self.load()
        if not inputs:
            return []

        deps = self._require_deps()
        session = self._require_session()
        input_name = self._require_input_name()
        output_name = self._require_output_name()
        try:
            tensors = [self._preprocess_input(item) for item in inputs]
            batch_tensor = deps.np.stack(tensors, axis=0).astype(
                deps.np.float32,
                copy=False,
            )
            outputs = session.run([output_name], {input_name: batch_tensor})
        except ClassifierError:
            raise
        except Exception as exc:
            raise ClassifierError("onnx inference failed") from exc

        score_rows = _score_rows(
            outputs,
            labels=tuple(self.profile.output_mapping),
            batch_size=len(inputs),
        )
        return [
            _raw_output_from_scores(classifier_input.asset_id, scores)
            for classifier_input, scores in zip(inputs, score_rows, strict=True)
        ]

    def _preprocess_input(self, classifier_input: ClassifierInput) -> Any:
        if not classifier_input.data:
            raise ClassifierError("onnx backend requires image bytes")

        deps = self._require_deps()
        try:
            with deps.image.open(BytesIO(classifier_input.data)) as image:
                image = deps.image_ops.exif_transpose(image)
                image = image.convert("RGB")
                image = image.resize(
                    (self.profile.input_size, self.profile.input_size)
                )
                array = deps.np.asarray(image, dtype=deps.np.float32) / 255.0
        except Exception as exc:
            raise ClassifierError("onnx image preprocessing failed") from exc

        mean = deps.np.asarray(self.profile.input_mean, dtype=deps.np.float32)
        std = deps.np.asarray(self.profile.input_std, dtype=deps.np.float32)
        array = (array - mean) / std
        array = deps.np.transpose(array, (2, 0, 1))
        return array.astype(deps.np.float32, copy=False)

    def _readable_model_path(self) -> Path:
        if not self.profile.model_path or not self.profile.model_path.strip():
            raise ClassifierBackendError(
                "onnx backend requires classifier_profiles model_path"
            )
        path = Path(self.profile.model_path)
        try:
            if path.is_file():
                return path
        except OSError as exc:
            raise ClassifierBackendError(
                "onnx model_path is not a readable file"
            ) from exc
        raise ClassifierBackendError("onnx model_path is not a readable file")

    def _require_deps(self) -> _OnnxDependencies:
        if self._deps is None:
            raise ClassifierError("onnx backend is not loaded")
        return self._deps

    def _require_session(self) -> Any:
        if self._session is None:
            raise ClassifierError("onnx backend is not loaded")
        return self._session

    def _require_input_name(self) -> str:
        if self._input_name is None:
            raise ClassifierError("onnx backend input is not configured")
        return self._input_name

    def _require_output_name(self) -> str:
        if self._output_name is None:
            raise ClassifierError("onnx backend output is not configured")
        return self._output_name


def _load_onnx_dependencies() -> _OnnxDependencies:
    try:
        import numpy as np
        import onnxruntime as ort
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ClassifierBackendError(
            "onnx backend requires optional dependencies; install mediarefinery[onnx]"
        ) from exc

    return _OnnxDependencies(ort=ort, np=np, image=Image, image_ops=ImageOps)


def _select_io_name(
    values: Sequence[Any],
    *,
    configured_name: str | None,
    kind: str,
) -> str:
    names = tuple(str(value.name) for value in values if getattr(value, "name", None))
    if configured_name:
        if configured_name in names:
            return configured_name
        raise ClassifierBackendError(f"onnx {kind}_name was not found in the model")
    if not names:
        raise ClassifierBackendError(f"onnx model has no usable {kind}")
    return names[0]


def _score_rows(
    outputs: object,
    *,
    labels: tuple[str, ...],
    batch_size: int,
) -> list[dict[str, float]]:
    if not isinstance(outputs, (list, tuple)) or not outputs:
        raise ClassifierError("onnx session returned no outputs")

    rows = _as_python(outputs[0])
    if batch_size == 1 and _is_number_sequence(rows):
        rows = [rows]
    if not isinstance(rows, list) or len(rows) != batch_size:
        raise ClassifierError("onnx output batch size did not match inputs")

    score_rows: list[dict[str, float]] = []
    for row in rows:
        scores = _flatten_numbers(row)
        if len(scores) != len(labels):
            raise ClassifierError(
                "onnx output label count did not match output_mapping"
            )
        score_rows.append(
            {label: float(score) for label, score in zip(labels, scores, strict=True)}
        )
    return score_rows


def _raw_output_from_scores(asset_id: str, scores: dict[str, float]) -> RawModelOutput:
    best_label = max(scores, key=scores.__getitem__)
    return RawModelOutput(
        asset_id=asset_id,
        raw_label=best_label,
        raw_labels=(best_label,),
        raw_scores=scores,
    )


def _as_python(value: object) -> object:
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def _flatten_numbers(value: object) -> list[float]:
    value = _as_python(value)
    if _is_number(value):
        return [float(value)]
    if not isinstance(value, list):
        raise ClassifierError("onnx output could not be converted to scores")

    flattened: list[float] = []
    for item in value:
        flattened.extend(_flatten_numbers(item))
    return flattened


def _is_number_sequence(value: object) -> bool:
    return isinstance(value, list) and all(_is_number(item) for item in value)


def _is_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))
