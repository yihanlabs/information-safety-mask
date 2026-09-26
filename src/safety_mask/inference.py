from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path

from .memory import GIB, process_sample, require_memory
from .models import Entity, OcrLine, Rect, Word
from .progress import ProgressCallback, ProgressEvent, detection_megapixels, detection_size
from .resources import thread_environment
from .tiling import TILING_ENABLED, DetectionAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_ROOT = PROJECT_ROOT / ".models"
OCR_NAMES = ("PP-OCRv5_server_det", "PP-OCRv5_server_rec")


class AccelerationCompatibilityError(RuntimeError):
    """A known native backend failure; no OCR text crosses the process boundary."""


def is_acceleration_error(exc):
    return "ConvertPirAttribute2RuntimeAttribute" in str(exc)


def configure_environment():
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(MODEL_ROOT / "paddlex")
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"


def model_status(root: Path = MODEL_ROOT) -> dict:
    required = [root / "paddlex" / "official_models" / name / "inference.pdiparams" for name in OCR_NAMES]
    required += [root / "zh_core_web_trf" / "config.cfg", root / "manifest.json"]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    return {"ready": not missing, "missing": missing}


def verify_model_files(root: Path = MODEL_ROOT, lock_path: Path = PROJECT_ROOT / "models.lock.json"):
    try:
        manifest = json.loads(lock_path.read_text(encoding="utf-8"))
        for relative, expected in manifest["files"].items():
            path = (root / relative).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("invalid model path")
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != expected:
                raise ValueError("model hash mismatch")
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError("本地模型未准备完成：文件校验失败，请重新准备模型。") from exc


def rectangle(box, width, height):
    values = list(map(float, box))
    x1, y1, x2, y2 = values
    x1, y1 = max(0, min(width - 1, x1)), max(0, min(height - 1, y1))
    x2, y2 = min(width, max(x1 + 1, x2)), min(height, max(y1 + 1, y2))
    return Rect(x=x1, y=y1, width=x2 - x1, height=y2 - y1)


def parse_ocr(result, width: int, height: int) -> list[OcrLine]:
    lines = []
    texts = result.get("rec_texts", [])
    boxes = result.get("rec_boxes", [])
    scores = result.get("rec_scores", [])
    word_texts = result.get("text_word", [])
    word_boxes = result.get("text_word_boxes", [])
    for index, text in enumerate(texts):
        if not str(text).strip():
            continue
        words, cursor = [], 0
        if index < len(word_texts) and index < len(word_boxes):
            for word_text, box in zip(word_texts[index], word_boxes[index]):
                word_text = str(word_text)
                start = str(text).find(word_text, cursor)
                if start < 0 or not word_text:
                    continue
                end = start + len(word_text)
                words.append(Word(start=start, end=end, box=rectangle(box, width, height)))
                cursor = end
        lines.append(
            OcrLine(
                id=f"line-{index}",
                text=str(text),
                confidence=float(scores[index]),
                box=rectangle(boxes[index], width, height),
                words=words,
            )
        )
    return lines


class _ObservedPredictor:
    """Observe the existing lazy predictor without changing its inputs or results."""

    def __init__(self, predictor, stage, report, units):
        self.predictor, self.stage, self.report, self.units = predictor, stage, report, units

    def __getattr__(self, name):
        return getattr(self.predictor, name)

    def __call__(self, images, **options):
        total = len(images)
        units = self.units if self.stage == "detecting" else total
        self.report(ProgressEvent(self.stage, total=total, units=units))
        for completed, result in enumerate(self.predictor(images, **options), 1):
            self.report(ProgressEvent(self.stage, completed, total, units))
            yield result


@contextmanager
def observe_ocr(ocr, report, megapixels, tiled=False):
    if report is None and not tiled:
        yield
        return
    # Adapter for the locked PaddleOCR 3.3.2 / PaddleX 3.3.13 CPU pipeline.
    # Restore the predictors even on error so callbacks never leak between images.
    pipeline = ocr.paddlex_pipeline._pipeline
    detection, recognition = pipeline.text_det_model, pipeline.text_rec_model
    pipeline.text_det_model = DetectionAdapter(detection, report, tiled)
    if report:
        pipeline.text_rec_model = _ObservedPredictor(recognition, "recognizing", report, 1)
    try:
        yield
    finally:
        pipeline.text_det_model, pipeline.text_rec_model = detection, recognition


class LocalEngine:
    def __init__(self, *, accelerated=True, threads=2, tiled=TILING_ENABLED, memory_saving=False):
        self.ocr = None
        self.nlp = None
        self.accelerated, self.threads, self.tiled = accelerated, threads, tiled
        self.memory_saving = memory_saving
        self.memory_report = None

    def check_memory(self, stage, additional=0, *, calibrated=False):
        current = require_memory(additional, stage)
        if self.memory_report:
            self.memory_report(
                {
                    **current.view(),
                    **process_sample(),
                    "stage": stage,
                    "mode": "saving" if self.memory_saving else "normal",
                    "estimated_additional_bytes": additional,
                    "estimate_basis": "measured_loading" if calibrated else "allocation_only",
                    "workspace_estimate_known": calibrated,
                }
            )

    def load(self, on_progress: ProgressCallback | None = None):
        if self.ocr is not None:
            return
        if on_progress:
            on_progress(ProgressEvent("loading", total=1))
        configure_environment()
        thread_environment(self.threads)
        if not model_status()["ready"]:
            raise RuntimeError("本地模型未准备完成，请运行“安装与准备.cmd”；当前图片可手动遮盖并检查。")
        verify_model_files()
        # Pinned CPU loader envelope, excluding the deferred entity model.
        # require_memory adds 25% headroom and 1 GiB system reserve.
        self.check_memory("loading", 2 * GIB, calibrated=True)
        import cv2
        from paddleocr import PaddleOCR

        cv2.setNumThreads(1)
        self.ocr = PaddleOCR(
            text_detection_model_name=OCR_NAMES[0],
            text_detection_model_dir=str(MODEL_ROOT / "paddlex" / "official_models" / OCR_NAMES[0]),
            text_recognition_model_name=OCR_NAMES[1],
            text_recognition_model_dir=str(MODEL_ROOT / "paddlex" / "official_models" / OCR_NAMES[1]),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            return_word_box=True,
            text_rec_score_thresh=0.0,
            device="cpu",
            cpu_threads=self.threads,
            enable_mkldnn=self.accelerated,
            mkldnn_cache_capacity=1 if self.memory_saving else 10,
            text_recognition_batch_size=1 if self.memory_saving else 6,
        )
        if on_progress:
            on_progress(ProgressEvent("loading", 1, 1))

    def analyze(
        self, source: bytes, on_progress: ProgressCallback | None = None
    ) -> tuple[list[OcrLine], list[Entity]]:
        self.load(on_progress)
        import numpy as np
        from PIL import Image

        from .raster import Raster

        if isinstance(source, Raster):
            lines = self._raster_ocr(source, on_progress)
            return self._entities(lines, on_progress)

        with Image.open(io.BytesIO(source)) as image:
            width, height = image.size
            self.check_memory("preparing", width * height * 12)
            pixels = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
        with observe_ocr(self.ocr, on_progress, detection_megapixels(width, height), self.tiled):
            try:
                result = next(iter(self.ocr.predict(pixels)))
            except KeyError as exc:
                if exc.args != ("text_word_region",):
                    raise
                # PaddleX 3.3.x omits this field on text-free input when word boxes are enabled.
                # Rerun without word boxes; any detected text uses conservative full-line masks.
                result = next(iter(self.ocr.predict(pixels, return_word_box=False)))
        lines = parse_ocr(result, width, height)
        return self._entities(lines, on_progress)

    def _raster_ocr(self, source, report):
        import math

        import numpy as np
        from paddlex.inference.pipelines.components import cal_ocr_word_box, convert_points_to_boxes

        pipeline = self.ocr.paddlex_pipeline._pipeline
        cw, ch = detection_size(source.width, source.height)
        # Native detector workspace varies by shape/backend. Do not turn an
        # uncalibrated whole-model guess into a hard admission threshold.
        self.check_memory("detecting", cw * ch * 12)
        if report:
            report(ProgressEvent("detecting", total=1, units=cw * ch / 1e6))
        pixels = source.resized(cw, ch)[:, :, ::-1].copy()
        detected = next(iter(pipeline.text_det_model([pixels], **pipeline.get_text_det_params())))
        polygons = np.asarray(detected["dt_polys"], dtype=np.float32).reshape(-1, 4, 2)
        polygons[:, :, 0] *= source.width / cw
        polygons[:, :, 1] *= source.height / ch
        polygons = pipeline._sort_boxes(polygons)
        del pixels, detected
        if report:
            report(ProgressEvent("detecting", 1, 1, cw * ch / 1e6))
            report(ProgressEvent("recognizing", total=len(polygons), units=len(polygons)))
        result = {"rec_texts": [], "rec_scores": [], "rec_boxes": [], "text_word": [], "text_word_boxes": []}
        # Sorting by estimated crop aspect ratio retains the locked recognizer's batching behaviour.
        order = sorted(
            range(len(polygons)),
            key=lambda i: (
                np.linalg.norm(polygons[i][0] - polygons[i][1])
                / max(1, np.linalg.norm(polygons[i][0] - polygons[i][3]))
            ),
        )
        recognized = {}
        completed = 0
        batch_size = 1 if self.memory_saving else 6
        with source.blob.open() as reader:
            for start in range(0, len(order), batch_size):
                indices, crops = order[start : start + batch_size], []
                estimated = sum(
                    (float(polygons[i][:, 0].max() - polygons[i][:, 0].min()) + 6)
                    * (float(polygons[i][:, 1].max() - polygons[i][:, 1].min()) + 6)
                    * 12
                    for i in indices
                )
                self.check_memory("recognizing", int(estimated))
                for index in indices:
                    poly = polygons[index]
                    x = max(0, math.floor(float(poly[:, 0].min())) - 2)
                    y = max(0, math.floor(float(poly[:, 1].min())) - 2)
                    right = min(source.width, math.ceil(float(poly[:, 0].max())) + 3)
                    bottom = min(source.height, math.ceil(float(poly[:, 1].max())) + 3)
                    with source.read_rect(x, y, right - x, bottom - y, reader) as region:
                        array = np.asarray(region)[:, :, ::-1].copy()
                    crops.append(next(iter(pipeline._crop_by_polys(array, [poly - [x, y]]))))
                    del array
                for index, prediction in zip(indices, pipeline.text_rec_model(crops, return_word_box=True)):
                    text, word_info = prediction["rec_text"]
                    words, boxes = cal_ocr_word_box(text, polygons[index], word_info)
                    recognized[index] = (
                        text,
                        float(prediction["rec_score"]),
                        words,
                        convert_points_to_boxes(boxes),
                    )
                    completed += 1
                    if report:
                        report(ProgressEvent("recognizing", completed, len(polygons), len(polygons)))
                del crops
        for index, poly in enumerate(polygons):
            text, score, words, boxes = recognized[index]
            result["rec_texts"].append(text)
            result["rec_scores"].append(score)
            result["rec_boxes"].append(convert_points_to_boxes([poly])[0])
            result["text_word"].append(words)
            result["text_word_boxes"].append(boxes)
        return parse_ocr(result, source.width, source.height)

    def _entities(self, lines, on_progress):
        entities = []
        if on_progress:
            on_progress(ProgressEvent("analyzing", total=len(lines), units=len(lines)))
        if not lines:
            return lines, entities
        if self.nlp is None:
            self.check_memory("loading_entities", GIB, calibrated=True)
            import spacy
            import torch

            torch.set_num_threads(self.threads)
            if torch.get_num_interop_threads() != 1:
                torch.set_num_interop_threads(1)
            self.nlp = spacy.load(
                MODEL_ROOT / "zh_core_web_trf",
                exclude=["tagger", "parser", "attribute_ruler"],
                config={"components.transformer.model.with_spans.batch_size": 1}
                if self.memory_saving
                else {},
            )
        self.check_memory("analyzing")
        # Pipeline batches avoid repeatedly invoking the transformer per text line.
        for completed, (line, document) in enumerate(
            zip(
                lines,
                self.nlp.pipe((line.text for line in lines), batch_size=1 if self.memory_saving else 16),
            ),
            1,
        ):
            require_memory(0, "analyzing")
            for entity in document.ents:
                if entity.label_ in {"PERSON", "GPE", "LOC", "FAC"}:
                    entities.append(
                        Entity(
                            line_id=line.id, start=entity.start_char, end=entity.end_char, label=entity.label_
                        )
                    )
            if on_progress:
                on_progress(ProgressEvent("analyzing", completed, len(lines), len(lines)))
        return lines, entities
