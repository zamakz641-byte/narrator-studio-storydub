from __future__ import annotations

import gc
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

MODEL = None
MODEL_PATH = ""
DEVICE = ""
COMPUTE_TYPE = ""
_DLL_HANDLES: list[Any] = []
_CUDA_DLL_DIRS: list[str] = []


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _remember_dll_dir(path: Path, rows: list[Path]) -> None:
    try:
        p = path.resolve()
    except Exception:
        return
    if not p.is_dir() or p in rows:
        return
    rows.append(p)


def _bootstrap_cuda_dll_paths() -> list[str]:
    """Expose CUDA/cuBLAS/cuDNN DLLs already installed in this venv.

    Nothing is downloaded or installed. Pip CUDA wheels commonly place DLLs in
    Lib/site-packages/nvidia/*/bin, while Torch keeps some DLLs in torch/lib.
    Windows does not always add those directories to PATH for child processes.
    """
    global _CUDA_DLL_DIRS
    if _CUDA_DLL_DIRS:
        return list(_CUDA_DLL_DIRS)

    candidates: list[Path] = []
    roots: list[Path] = []
    for raw in (sys.prefix, sys.base_prefix, Path(sys.executable).parent.parent):
        try:
            p = Path(raw).resolve()
        except Exception:
            continue
        if p.is_dir() and p not in roots:
            roots.append(p)

    for root in roots:
        site = root / "Lib" / "site-packages"
        for p in (
            site / "nvidia" / "cublas" / "bin",
            site / "nvidia" / "cudnn" / "bin",
            site / "nvidia" / "cuda_runtime" / "bin",
            site / "nvidia" / "cuda_nvrtc" / "bin",
            site / "torch" / "lib",
            root / "Library" / "bin",
            root / "DLLs",
        ):
            _remember_dll_dir(p, candidates)

        # Some wheel layouts differ slightly. Search only inside this venv,
        # never the whole disk, for the specific CUDA 12 DLLs Faster-Whisper uses.
        if site.is_dir():
            for dll_name in (
                "cublas64_12.dll",
                "cublasLt64_12.dll",
                "cudnn64_9.dll",
                "cudnn_ops64_9.dll",
            ):
                try:
                    for dll in site.rglob(dll_name):
                        _remember_dll_dir(dll.parent, candidates)
                        break
                except OSError:
                    pass

    for env_name in ("CUDA_PATH", "CUDA_PATH_V12_8", "CUDA_PATH_V12_6", "CUDA_PATH_V12_4", "CUDA_PATH_V12_1"):
        raw = os.environ.get(env_name)
        if raw:
            _remember_dll_dir(Path(raw) / "bin", candidates)

    existing = [x for x in os.environ.get("PATH", "").split(os.pathsep) if x]
    prepend = [str(p) for p in candidates if str(p) not in existing]
    if prepend:
        os.environ["PATH"] = os.pathsep.join(prepend + existing)

    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        for p in candidates:
            try:
                _DLL_HANDLES.append(os.add_dll_directory(str(p)))
            except OSError:
                pass

    _CUDA_DLL_DIRS = [str(p) for p in candidates]
    return list(_CUDA_DLL_DIRS)


# Do this before importing faster_whisper / ctranslate2.
_bootstrap_cuda_dll_paths()


def _make_model(path: Path, device: str, compute_type: str):
    from faster_whisper import WhisperModel

    try:
        return WhisperModel(
            str(path),
            device=device,
            compute_type=compute_type,
            local_files_only=True,
            cpu_threads=4,
        )
    except TypeError as type_exc:
        if "local_files_only" not in str(type_exc):
            raise
        return WhisperModel(
            str(path),
            device=device,
            compute_type=compute_type,
            cpu_threads=4,
        )


def _set_model(path: Path, device: str, compute_type: str):
    global MODEL, MODEL_PATH, DEVICE, COMPUTE_TYPE
    MODEL = _make_model(path, device, compute_type)
    MODEL_PATH = str(path)
    DEVICE = device
    COMPUTE_TYPE = compute_type


def _release_model() -> None:
    global MODEL, DEVICE, COMPUTE_TYPE
    MODEL = None
    DEVICE = ""
    COMPUTE_TYPE = ""
    gc.collect()


def load_model(model_path: str) -> dict[str, Any]:
    global MODEL, MODEL_PATH, DEVICE, COMPUTE_TYPE
    path = Path(model_path).resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"Faster-Whisper model not found: {path}")
    if MODEL is not None and MODEL_PATH == str(path):
        return {
            "loaded": True,
            "model_path": MODEL_PATH,
            "device": DEVICE,
            "compute_type": COMPUTE_TYPE,
            "cold": False,
            "cuda_dll_dirs": _CUDA_DLL_DIRS,
        }

    t0 = time.perf_counter()
    errors: list[str] = []
    for device, compute_type in (("cuda", "int8_float16"), ("cuda", "float16"), ("cpu", "int8")):
        try:
            _set_model(path, device, compute_type)
            return {
                "loaded": True,
                "model_path": MODEL_PATH,
                "device": DEVICE,
                "compute_type": COMPUTE_TYPE,
                "cold": True,
                "load_seconds": round(time.perf_counter() - t0, 3),
                "fallback_errors": errors,
                "cuda_dll_dirs": _CUDA_DLL_DIRS,
            }
        except Exception as exc:
            _release_model()
            errors.append(f"{device}/{compute_type}: {exc}")
    raise RuntimeError("Faster-Whisper could not load locally: " + " | ".join(errors[-3:]))


def _run_transcribe(audio_path: Path, language: str):
    segments, info = MODEL.transcribe(
        str(audio_path),
        language=language,
        beam_size=1,
        best_of=1,
        temperature=0.0,
        vad_filter=False,
        condition_on_previous_text=False,
        word_timestamps=False,
    )
    # Force generator evaluation here so CUDA DLL failures are caught here,
    # instead of escaping later while joining segment text.
    segments = list(segments)
    text = " ".join(str(seg.text or "").strip() for seg in segments if str(seg.text or "").strip()).strip()
    return text, info


def _looks_like_cuda_runtime_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    needles = (
        "cublas",
        "cudnn",
        "cuda",
        "cublas64_12.dll",
        "cudnn_ops64_9.dll",
        "library",
        "dll is not found",
        "cannot be loaded",
    )
    return any(x in msg for x in needles)


def transcribe(req: dict[str, Any]) -> dict[str, Any]:
    rid = str(req.get("request_id") or "")
    model_path = str(req.get("model_path") or "")
    audio_path = Path(str(req.get("audio_path") or "")).resolve()
    language = str(req.get("language") or "en").lower().split("-", 1)[0]
    if not audio_path.is_file():
        raise FileNotFoundError(f"QC audio not found: {audio_path}")

    init = load_model(model_path)
    emit({"event": "qc_runtime", "request_id": rid, **init})

    t0 = time.perf_counter()
    fallback_reason = ""
    try:
        text, info = _run_transcribe(audio_path, language)
    except Exception as exc:
        # CTranslate2 can instantiate a CUDA model successfully and only try to
        # load cuBLAS on the first actual inference. Retry once on CPU rather than
        # making voice cloning fail because Windows forgot a DLL search path.
        if DEVICE != "cuda" or not _looks_like_cuda_runtime_error(exc):
            raise
        fallback_reason = str(exc)
        emit({
            "event": "qc_log",
            "request_id": rid,
            "message": "CUDA Faster-Whisper indisponible, fallback CPU int8: " + fallback_reason[-900:],
        })
        path = Path(model_path).resolve()
        _release_model()
        _set_model(path, "cpu", "int8")
        emit({
            "event": "qc_runtime",
            "request_id": rid,
            "loaded": True,
            "model_path": MODEL_PATH,
            "device": DEVICE,
            "compute_type": COMPUTE_TYPE,
            "cold": True,
            "fallback_reason": fallback_reason,
            "cuda_dll_dirs": _CUDA_DLL_DIRS,
        })
        text, info = _run_transcribe(audio_path, language)

    return {
        "event": "transcribed",
        "request_id": rid,
        "ok": True,
        "text": text,
        "language": getattr(info, "language", language),
        "language_probability": round(float(getattr(info, "language_probability", 0.0) or 0.0), 4),
        "elapsed": round(time.perf_counter() - t0, 3),
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
        "fallback_reason": fallback_reason,
        "cuda_dll_dirs": _CUDA_DLL_DIRS,
    }


def main() -> int:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        rid = ""
        try:
            req = json.loads(raw)
            rid = str(req.get("request_id") or "")
            cmd = str(req.get("cmd") or "")
            if cmd == "shutdown":
                emit({"event": "shutdown", "request_id": rid, "ok": True})
                return 0
            if cmd == "init":
                result = load_model(str(req["model_path"]))
                emit({"event": "ready", "request_id": rid, "ok": True, **result})
                continue
            if cmd == "transcribe":
                emit(transcribe(req))
                continue
            raise ValueError(f"Unknown QC command: {cmd}")
        except Exception as exc:
            emit({
                "event": "error",
                "request_id": rid,
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc()[-5000:],
            })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
