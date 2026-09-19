from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# Keep this runtime strictly local/offline. The model already exists in DubRoom.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DEACTIVATE_ASYNC_LOAD", "1")
os.environ.setdefault("HF_ENABLE_PARALLEL_LOADING", "false")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

MODEL = None
PROMPTS: dict[str, Any] = {}
MODEL_PATH = ""
SAMPLE_RATE = 24000


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _to_numpy(audio: Any):
    import numpy as np
    import torch

    if isinstance(audio, (list, tuple)):
        if not audio:
            raise RuntimeError("OmniVoice returned an empty audio list")
        audio = audio[0]
    if torch.is_tensor(audio):
        audio = audio.detach().float().cpu().numpy()
    arr = np.asarray(audio, dtype=np.float32).squeeze()
    if arr.ndim != 1 or arr.size == 0:
        raise RuntimeError(f"Unexpected OmniVoice audio shape: {arr.shape}")
    return arr


def load_model(model_path: str) -> dict[str, Any]:
    global MODEL, MODEL_PATH, SAMPLE_RATE
    path = Path(model_path).resolve()
    if MODEL is not None:
        if MODEL_PATH != str(path):
            raise RuntimeError(f"OmniVoice worker already loaded another model: {MODEL_PATH}")
        return {"loaded": True, "model_path": MODEL_PATH, "sample_rate": SAMPLE_RATE, "cold": False}
    if not path.is_dir():
        raise FileNotFoundError(f"Local OmniVoice model not found: {path}")

    import torch
    from omnivoice import OmniVoice

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable in the OmniVoice HQ runtime")

    t0 = time.perf_counter()
    torch.set_num_threads(1)
    torch.backends.mkldnn.enabled = False
    # Loading directly through device_map intermittently access-violates in
    # Transformers 5.x on Windows. Materialize sequentially on CPU, then move
    # the complete FP16 model (including the audio tokenizer) to the RTX GPU.
    MODEL = OmniVoice.from_pretrained(
        str(path),
        dtype=torch.float16,
        load_asr=False,
        low_cpu_mem_usage=False,
    )
    # Keep the tokenizer submodules in their native dtype. Forcing the entire
    # hierarchy to FP16 can damage prompt/audio preprocessing; the model was
    # already materialized with FP16 weights above.
    MODEL = MODEL.to("cuda:0")
    torch.cuda.synchronize()
    MODEL_PATH = str(path)
    SAMPLE_RATE = int(getattr(MODEL, "sampling_rate", None) or 24000)
    return {
        "loaded": True,
        "model_path": MODEL_PATH,
        "sample_rate": SAMPLE_RATE,
        "cold": True,
        "load_seconds": round(time.perf_counter() - t0, 3),
        "gpu": torch.cuda.get_device_name(0),
    }


def load_prompt(voice_id: str, prompt_path: str):
    key = f"{voice_id}|{Path(prompt_path).resolve()}"
    if key in PROMPTS:
        return PROMPTS[key]
    path = Path(prompt_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Narrator prompt not found: {path}")

    from omnivoice import VoiceClonePrompt

    prompt = VoiceClonePrompt.load(str(path))
    PROMPTS[key] = prompt
    return prompt


def generate_chunk(req: dict[str, Any]) -> dict[str, Any]:
    """Render one already-sanitized chunk.

    v1.4 deliberately moves chunking/retry/QC orchestration to app.py. Keeping
    this worker to a single chunk makes a failed phrase replaceable without
    regenerating the entire script.
    """
    global SAMPLE_RATE
    import soundfile as sf
    import torch

    request_id = str(req.get("request_id") or "")
    model_path = str(req["model_path"])
    voice_id = str(req["voice_id"])
    prompt_path = str(req["prompt_path"])
    output_path = Path(str(req["output_path"])).resolve()
    text = str(req.get("text") or "").strip()
    language = str(req.get("language") or "en").lower().split("-", 1)[0]
    speed = max(0.5, min(2.0, float(req.get("speed") or 1.0)))
    steps = max(8, min(64, int(req.get("steps") or 12)))
    seed = int(req.get("seed") if req.get("seed") is not None else int(time.time_ns() & 0x7FFFFFFF))
    pad_duration = max(0.0, min(1.0, float(req.get("pad_duration") if req.get("pad_duration") is not None else 0.12)))
    fade_duration = max(0.0, min(0.5, float(req.get("fade_duration") if req.get("fade_duration") is not None else 0.002)))

    if not text:
        raise ValueError("Text is empty")

    init = load_model(model_path)
    emit({"event": "runtime", "request_id": request_id, **init})
    prompt = load_prompt(voice_id, prompt_path)

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
        torch.cuda.synchronize()

    started = time.perf_counter()
    audio = MODEL.generate(
        text=text,
        language=language,
        voice_clone_prompt=prompt,
        num_step=steps,
        guidance_scale=2.0,
        denoise=True,
        t_shift=0.1,
        position_temperature=5.0,
        class_temperature=0.0,
        layer_penalty_factor=5.0,
        speed=speed,
        normalize_text=False,
        postprocess_output=True,
        pad_duration=pad_duration,
        fade_duration=fade_duration,
    )
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    arr = _to_numpy(audio)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_name(output_path.stem + ".tmp.wav")
    sf.write(str(tmp), arr, SAMPLE_RATE, subtype="PCM_16")
    os.replace(tmp, output_path)

    duration = arr.size / float(SAMPLE_RATE)
    return {
        "event": "completed",
        "request_id": request_id,
        "ok": True,
        "output_path": str(output_path),
        "duration": round(duration, 3),
        "sample_rate": SAMPLE_RATE,
        "elapsed": round(time.perf_counter() - started, 3),
        "voice_id": voice_id,
        "speed": speed,
        "steps": steps,
        "seed": seed,
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
            if cmd in {"generate", "generate_chunk"}:
                emit(generate_chunk(req))
                continue
            raise ValueError(f"Unknown command: {cmd}")
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
