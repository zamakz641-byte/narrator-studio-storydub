from __future__ import annotations

import json
import os
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

# Normal inference is strictly local/offline. The model download is orchestrated
# by app.py in a separate one-shot process.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

MODEL = None
MODEL_PATH = ""
SAMPLE_RATE = 24000
PROMPT_CACHE: dict[str, Any] = {}


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _language_name(value: Any) -> str:
    raw = str(value or "Auto").strip().lower().split("-", 1)[0]
    return {
        "fr": "French",
        "en": "English",
        "de": "German",
        "es": "Spanish",
        "it": "Italian",
        "pt": "Portuguese",
        "ru": "Russian",
        "ja": "Japanese",
        "ko": "Korean",
        "zh": "Chinese",
        "auto": "Auto",
    }.get(raw, str(value or "Auto"))


def load_model(model_path: str) -> dict[str, Any]:
    global MODEL, MODEL_PATH
    path = Path(model_path).expanduser().resolve()
    if MODEL is not None:
        if MODEL_PATH != str(path):
            raise RuntimeError(f"Qwen3-TTS worker already loaded another model: {MODEL_PATH}")
        return {"loaded": True, "model_path": MODEL_PATH, "cold": False}
    if not path.is_dir() or not (path / "config.json").is_file():
        raise FileNotFoundError(f"Local Qwen3-TTS 0.6B Base model not found: {path}")

    import torch
    from qwen_tts import Qwen3TTSModel

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable in the Qwen3-TTS runtime")

    t0 = time.perf_counter()
    dtype = torch.bfloat16 if bool(getattr(torch.cuda, "is_bf16_supported", lambda: False)()) else torch.float16
    errors: list[str] = []
    # Do not make FlashAttention a hard dependency. Try it when already installed,
    # otherwise SDPA is the safest path on a 4060 Laptop.
    for attention in ("flash_attention_2", "sdpa", None):
        try:
            kwargs = {
                "device_map": "cuda:0",
                "dtype": dtype,
            }
            if attention:
                kwargs["attn_implementation"] = attention
            MODEL = Qwen3TTSModel.from_pretrained(str(path), **kwargs)
            torch.cuda.synchronize()
            MODEL_PATH = str(path)
            return {
                "loaded": True,
                "model_path": MODEL_PATH,
                "cold": True,
                "load_seconds": round(time.perf_counter() - t0, 3),
                "gpu": torch.cuda.get_device_name(0),
                "dtype": str(dtype).replace("torch.", ""),
                "attention": attention or "default",
                "fallback_errors": errors,
            }
        except Exception as exc:
            MODEL = None
            errors.append(f"{attention or 'default'}: {exc}")
    raise RuntimeError("Qwen3-TTS could not load locally: " + " | ".join(errors[-3:]))


def _voice_item_cls():
    try:
        from qwen_tts import VoiceClonePromptItem
        return VoiceClonePromptItem
    except Exception:
        # Compatibility with package layouts that do not re-export the dataclass.
        from qwen_tts.inference.qwen3_tts_model import VoiceClonePromptItem
        return VoiceClonePromptItem


def _restore_prompt(path: Path):
    key = str(path.resolve())
    if key in PROMPT_CACHE:
        return PROMPT_CACHE[key]

    import torch
    VoiceClonePromptItem = _voice_item_cls()

    try:
        payload = torch.load(str(path), map_location="cpu", weights_only=True)
    except TypeError:
        payload = torch.load(str(path), map_location="cpu")
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError(f"Invalid Qwen voice prompt: {path}")

    items = []
    for d in payload["items"]:
        if not isinstance(d, dict):
            raise RuntimeError(f"Invalid Qwen prompt item in {path}")
        ref_code = d.get("ref_code")
        if ref_code is not None and not torch.is_tensor(ref_code):
            ref_code = torch.tensor(ref_code)
        ref_spk = d.get("ref_spk_embedding")
        if ref_spk is None:
            raise RuntimeError(f"Missing ref_spk_embedding in {path}")
        if not torch.is_tensor(ref_spk):
            ref_spk = torch.tensor(ref_spk)
        items.append(
            VoiceClonePromptItem(
                ref_code=ref_code,
                ref_spk_embedding=ref_spk,
                x_vector_only_mode=bool(d.get("x_vector_only_mode", False)),
                icl_mode=bool(d.get("icl_mode", not bool(d.get("x_vector_only_mode", False)))),
                ref_text=d.get("ref_text"),
            )
        )
    if not items:
        raise RuntimeError(f"Empty Qwen voice prompt: {path}")
    PROMPT_CACHE[key] = items
    return items


def _save_prompt(items: Any, output: Path, *, mode: str, ref_text: str) -> None:
    import torch
    payload = {
        "format": "qwen3-tts-voice-clone-v1",
        "mode": mode,
        "ref_text": ref_text,
        "items": [asdict(it) for it in items],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    torch.save(payload, str(tmp))
    os.replace(tmp, output)
    PROMPT_CACHE.pop(str(output.resolve()), None)


def create_prompts(req: dict[str, Any]) -> dict[str, Any]:
    rid = str(req.get("request_id") or "")
    model_path = str(req.get("model_path") or "")
    ref_audio = Path(str(req.get("ref_audio_path") or "")).expanduser().resolve()
    ref_text = str(req.get("ref_text") or "").strip()
    icl_output = Path(str(req.get("icl_prompt_path") or req.get("output_prompt_path") or "")).expanduser().resolve()
    xvec_output = Path(str(req.get("xvec_prompt_path") or "")).expanduser().resolve()
    if not ref_audio.is_file():
        raise FileNotFoundError(f"Reference audio not found: {ref_audio}")
    if not ref_text:
        raise ValueError("Reference transcript is empty. Qwen ICL cloning needs ref_text.")
    if not str(icl_output):
        raise ValueError("ICL output path is empty")

    init = load_model(model_path)
    emit({"event": "qwen_runtime", "request_id": rid, **init})
    t0 = time.perf_counter()

    # High-fidelity ICL prompt: reference codes + speaker embedding + exact text.
    icl_items = MODEL.create_voice_clone_prompt(
        ref_audio=str(ref_audio),
        ref_text=ref_text,
        x_vector_only_mode=False,
    )
    _save_prompt(icl_items, icl_output, mode="icl", ref_text=ref_text)

    # Tiny x-vector fallback: less timbre detail, but useful if an ICL prompt ever
    # leaks/repeats reference content on a difficult phrase.
    if str(xvec_output):
        xvec_items = MODEL.create_voice_clone_prompt(
            ref_audio=str(ref_audio),
            ref_text=None,
            x_vector_only_mode=True,
        )
        _save_prompt(xvec_items, xvec_output, mode="xvector", ref_text="")

    return {
        "event": "prompt_created",
        "request_id": rid,
        "ok": True,
        "icl_prompt_path": str(icl_output),
        "xvec_prompt_path": str(xvec_output) if str(xvec_output) else "",
        "ref_text": ref_text,
        "elapsed": round(time.perf_counter() - t0, 3),
    }


def generate_chunk(req: dict[str, Any]) -> dict[str, Any]:
    global SAMPLE_RATE
    import numpy as np
    import soundfile as sf
    import torch

    rid = str(req.get("request_id") or "")
    model_path = str(req.get("model_path") or "")
    prompt_path = Path(str(req.get("prompt_path") or "")).expanduser().resolve()
    output_path = Path(str(req.get("output_path") or "")).expanduser().resolve()
    text = str(req.get("text") or "").strip()
    language = _language_name(req.get("language") or "Auto")
    seed = int(req.get("seed") if req.get("seed") is not None else int(time.time_ns() & 0x7FFFFFFF))
    temperature = max(0.2, min(1.5, float(req.get("temperature") or 0.9)))
    repetition_penalty = max(1.0, min(1.5, float(req.get("repetition_penalty") or 1.05)))
    max_new_tokens = max(256, min(4096, int(req.get("max_new_tokens") or 2048)))

    if not text:
        raise ValueError("Text is empty")
    if not prompt_path.is_file():
        raise FileNotFoundError(f"Qwen cloned voice prompt not found: {prompt_path}")

    init = load_model(model_path)
    emit({"event": "qwen_runtime", "request_id": rid, **init})
    prompt_items = _restore_prompt(prompt_path)

    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.cuda.synchronize()
    started = time.perf_counter()
    kwargs = dict(
        text=text,
        language=language,
        voice_clone_prompt=prompt_items,
        non_streaming_mode=True,
        do_sample=True,
        top_k=50,
        top_p=1.0,
        temperature=temperature,
        repetition_penalty=repetition_penalty,
        subtalker_dosample=True,
        subtalker_top_k=50,
        subtalker_top_p=1.0,
        subtalker_temperature=temperature,
        max_new_tokens=max_new_tokens,
    )
    try:
        wavs, sr = MODEL.generate_voice_clone(**kwargs)
    except TypeError as exc:
        # Older qwen_tts builds may not expose every sampling knob. Remove only
        # optional keys while preserving the reusable .pt prompt architecture.
        optional = ("subtalker_dosample", "subtalker_top_k", "subtalker_top_p", "subtalker_temperature")
        if not any(k in str(exc) for k in optional):
            raise
        for key in optional:
            kwargs.pop(key, None)
        wavs, sr = MODEL.generate_voice_clone(**kwargs)
    torch.cuda.synchronize()

    if not wavs:
        raise RuntimeError("Qwen3-TTS returned no audio")
    arr = np.asarray(wavs[0], dtype=np.float32).squeeze()
    if arr.ndim != 1 or arr.size == 0:
        raise RuntimeError(f"Unexpected Qwen3-TTS audio shape: {arr.shape}")
    SAMPLE_RATE = int(sr or 24000)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_name(output_path.stem + ".tmp.wav")
    sf.write(str(tmp), arr, SAMPLE_RATE, subtype="PCM_16")
    os.replace(tmp, output_path)
    return {
        "event": "completed",
        "request_id": rid,
        "ok": True,
        "output_path": str(output_path),
        "duration": round(arr.size / float(SAMPLE_RATE), 3),
        "sample_rate": SAMPLE_RATE,
        "elapsed": round(time.perf_counter() - started, 3),
        "seed": seed,
        "temperature": temperature,
        "repetition_penalty": repetition_penalty,
        "prompt_path": str(prompt_path),
        "text": text,
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
            if cmd in {"create_prompt", "create_prompts"}:
                emit(create_prompts(req))
                continue
            if cmd in {"generate", "generate_chunk"}:
                emit(generate_chunk(req))
                continue
            raise ValueError(f"Unknown Qwen command: {cmd}")
        except Exception as exc:
            emit({
                "event": "error",
                "request_id": rid,
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc()[-6000:],
            })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
