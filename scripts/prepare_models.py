"""Online preparation only. The application itself never downloads models."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / ".models"
os.environ["PADDLE_PDX_CACHE_HOME"] = str(MODELS / "paddlex")
os.environ["PADDLE_PDX_MODEL_SOURCE"] = "BOS"
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
os.environ["DISABLE_MODEL_SOURCE_CHECK"] = "True"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


def main():
    from paddlex.inference.utils.official_models import official_models

    for name in ("PP-OCRv5_server_det", "PP-OCRv5_server_rec"):
        print(f"准备 {name} ...", flush=True)
        directory = Path(official_models[name])
        if not (directory / "inference.pdiparams").is_file():
            raise RuntimeError(f"{name} 模型文件不完整")
    spec = importlib.util.find_spec("zh_core_web_trf")
    if not spec or not spec.origin:
        raise RuntimeError("请先安装锁定的 Python 依赖")
    source = Path(spec.origin).parent / "zh_core_web_trf-3.8.0"
    target = MODELS / "zh_core_web_trf"
    if not (target / "config.cfg").is_file():
        print("准备本地中文实体识别模型 ...", flush=True)
        shutil.copytree(source, target, dirs_exist_ok=True)
    # Record a reproducible local model inventory without any user data.
    entries = {}
    roots = [
        MODELS / "paddlex" / "official_models" / name
        for name in ("PP-OCRv5_server_det", "PP-OCRv5_server_rec")
    ]
    roots.append(target)
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and ".cache" not in path.parts:
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                entries[path.relative_to(MODELS).as_posix()] = digest
    manifest = {"version": 1, "paddleocr": "3.3.2", "ner": "zh_core_web_trf-3.8.0", "files": entries}
    pinned = ROOT / "models.lock.json"
    if pinned.is_file():
        expected = json.loads(pinned.read_text(encoding="utf-8"))["files"]
        if entries != expected:
            raise RuntimeError(
                "Model hashes differ from models.lock.json. Please check the local model files."
            )
    (MODELS / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("本地模型准备完成。后续处理可断网运行。", flush=True)


if __name__ == "__main__":
    main()
