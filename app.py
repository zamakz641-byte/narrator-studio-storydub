from __future__ import annotations

import base64
import json
import os
import sys
import importlib
import shutil
import socket
import random
import wave
import subprocess
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import webview

from quality_utils import (
    clean_text,
    qc_soft_floor,
    qc_threshold,
    split_text_aggressive,
    split_text_smart,
    speech_safe_variant,
    text_similarity,
    wav_sanity,
)

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HISTORY_DIR = ROOT / "history"
HISTORY_DB = ROOT / "history.json"
WEB_DIR = ROOT / "web"
WORKER_SCRIPT = ROOT / "tts_worker.py"
QWEN_WORKER_SCRIPT = ROOT / "qwen_tts_worker.py"
QC_WORKER_SCRIPT = ROOT / "qc_worker.py"
QWEN_VOICES_DIR = ROOT / "qwen_voices"
QWEN_MODEL_REPO = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
QWEN_MODEL_DEFAULT_DIR = ROOT / "models" / "Qwen3-TTS-12Hz-0.6B-Base"
QWEN_Q8_DEFAULT_DIR = ROOT / "models" / "Qwen3-TTS-0.6B-Q8"
QWEN_Q8_MODEL_NAME = "qwen3-tts-0.6b-q8_0.gguf"
QWEN_Q8_TOKENIZER_NAME = "qwen3-tts-tokenizer-q8_0.gguf"
QWEN_VOICES_DIR.mkdir(parents=True, exist_ok=True)
_PY_IMPORT_CACHE: dict[tuple[str, str], bool] = {}
WORK_DIR = HISTORY_DIR / ".work"
WORK_DIR.mkdir(parents=True, exist_ok=True)

HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def hidden_popen_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": si, "creationflags": int(getattr(subprocess, "CREATE_NO_WINDOW", 0))}


# Legacy StoryDub narrator identities. Current standalone configurations use the
# Dubroom voice library and fall back to the explicit entries in config.json.
STORYDUB_NARRATORS = (
    ("voice_ee40c6612ca1235e0c7a", "Male EN Narrator 1"),
    ("voice_73c9c20e2de552c1815d", "Male EN Narrator 2"),
)


def _existing_dir(value: Any) -> Path | None:
    try:
        p = Path(str(value or "")).expanduser().resolve()
    except Exception:
        return None
    return p if p.is_dir() else None


def _python_imports(python_path: Any, module: str) -> bool:
    try:
        py = Path(str(python_path or "")).expanduser().resolve()
    except Exception:
        return False
    if not py.is_file():
        return False
    cache_key = (str(py).lower(), str(module))
    if cache_key in _PY_IMPORT_CACHE:
        return _PY_IMPORT_CACHE[cache_key]
    try:
        proc = subprocess.run(
            [str(py), "-c", f"import {module}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            cwd=str(ROOT),
            **hidden_popen_kwargs(),
        )
        ok = proc.returncode == 0
    except Exception:
        ok = False
    _PY_IMPORT_CACHE[cache_key] = ok
    return ok


def _add_python_candidate(rows: list[Path], raw: Any) -> None:
    if not raw:
        return
    try:
        py = Path(str(raw)).expanduser().resolve()
    except Exception:
        return
    if py.is_file() and py not in rows:
        rows.append(py)


def _python_candidates(resolved: dict[str, Any]) -> list[Path]:
    """Find existing Python runtimes without installing anything.

    Qwen may live in a different venv than OmniVoice/Faster-Whisper, so only
    probing those two known runtimes is too narrow. Search StoryDub/DubRoom
    venvs plus Python installations already registered on Windows.
    """
    rows: list[Path] = []
    for raw in (
        resolved.get("qwen_runtime_python"),
        os.environ.get("QWEN_TTS_PYTHON"),
        sys.executable,
        resolved.get("runtime_python"),
        resolved.get("qc_runtime_python"),
    ):
        _add_python_candidate(rows, raw)

    roots: list[Path] = []
    for raw in (resolved.get("dubroom_root"), resolved.get("storydub_root"), ROOT, ROOT.parent):
        try:
            r = Path(str(raw or "")).expanduser().resolve()
        except Exception:
            continue
        if r.is_dir() and r not in roots:
            roots.append(r)

    patterns = (
        "data/environments/*/venv/Scripts/python.exe",
        "data/environments/*/.venv/Scripts/python.exe",
        "*/venv/Scripts/python.exe",
        "*/.venv/Scripts/python.exe",
        ".venv/Scripts/python.exe",
        "venv/Scripts/python.exe",
    )
    # Keep this bounded. We inspect only conventional environment locations,
    # not the user's whole disk like a deranged antivirus.
    for root in roots:
        for pattern in patterns:
            try:
                for py in root.glob(pattern):
                    _add_python_candidate(rows, py)
                    if len(rows) >= 40:
                        break
            except OSError:
                pass
            if len(rows) >= 40:
                break
        if len(rows) >= 40:
            break

    if os.name == "nt":
        # Include Python Launcher registered interpreters and PATH Pythons.
        try:
            proc = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=4, **hidden_popen_kwargs())
            for line in (proc.stdout or "").splitlines():
                candidate = line.strip().split()[-1] if line.strip() else ""
                _add_python_candidate(rows, candidate)
        except Exception:
            pass
        try:
            proc = subprocess.run(["where", "python"], capture_output=True, text=True, timeout=4, **hidden_popen_kwargs())
            for line in (proc.stdout or "").splitlines():
                _add_python_candidate(rows, line.strip())
        except Exception:
            pass
    return rows


def _find_python_with_module(resolved: dict[str, Any], module: str) -> tuple[Path | None, list[Path]]:
    candidates = _python_candidates(resolved)
    return next((py for py in candidates if _python_imports(py, module)), None), candidates


def _qwen_native_model_valid(path: Path) -> bool:
    """Validate a native Qwen3-TTS Base snapshot, not a Kobold/GGUF pack."""
    try:
        path = path.expanduser().resolve()
    except Exception:
        return False
    if not path.is_dir():
        return False
    if not (path / "config.json").is_file() or not (path / "model.safetensors").is_file():
        return False
    # The 0.6B Base snapshot embeds the speech tokenizer. Some package versions
    # name the directory slightly differently, so accept any populated tokenizer dir.
    tokenizer_dirs = [path / "speech_tokenizer", path / "speech-tokenizer"]
    if any(d.is_dir() and any(d.iterdir()) for d in tokenizer_dirs):
        return True
    # Older snapshots can resolve the tokenizer from preprocessor_config.json.
    # Do not reject those if the main native weights/config are definitely present.
    return (path / "preprocessor_config.json").is_file()


def _qwen_q8_pack_valid(path: Path) -> bool:
    try:
        path = path.expanduser().resolve()
    except Exception:
        return False
    return (
        path.is_dir()
        and (path / QWEN_Q8_MODEL_NAME).is_file()
        and (path / QWEN_Q8_TOKENIZER_NAME).is_file()
    )


def _add_model_candidate(rows: list[Path], raw: Any) -> None:
    if not raw:
        return
    try:
        path = Path(str(raw)).expanduser().resolve()
    except Exception:
        return
    if path not in rows:
        rows.append(path)


def _qwen_native_model_candidates(resolved: dict[str, Any]) -> list[Path]:
    """Search existing local Qwen native snapshots without crawling the whole disk.

    StoryDub/DubRoom has used several model folder naming conventions across patches,
    and Hugging Face may have already cached the official snapshot. v1.8 checks those
    locations before declaring the model missing.
    """
    rows: list[Path] = []
    for raw in (
        resolved.get("qwen_model_path"),
        QWEN_MODEL_DEFAULT_DIR,
    ):
        _add_model_candidate(rows, raw)

    model_roots: list[Path] = []
    for raw in (
        ROOT / "models",
        ROOT.parent / "models",
        Path(str(resolved.get("dubroom_root") or "")) / "models" if resolved.get("dubroom_root") else None,
        Path(str(resolved.get("dubroom_root") or "")) / "data" / "models" if resolved.get("dubroom_root") else None,
        Path(str(resolved.get("storydub_root") or "")) / "models" if resolved.get("storydub_root") else None,
        Path(str(resolved.get("storydub_root") or "")) / "data" / "models" if resolved.get("storydub_root") else None,
    ):
        if not raw:
            continue
        try:
            root = Path(raw).expanduser().resolve()
        except Exception:
            continue
        if root.is_dir() and root not in model_roots:
            model_roots.append(root)

    # Known names first, then bounded two-level discovery under model roots.
    name_patterns = (
        "Qwen3-TTS-12Hz-0.6B-Base",
        "qwen3-tts-12hz-0.6b-base",
        "tts-qwen3-0.6",
        "tts-qwen3-0.6b",
        "tts-qwen3-0.6-base",
        "*Qwen3*TTS*0.6*Base*",
        "*qwen3*tts*0.6*base*",
        "*tts*qwen3*0.6*",
    )
    for root in model_roots:
        for pattern in name_patterns:
            try:
                for item in root.glob(pattern):
                    _add_model_candidate(rows, item)
                    _add_model_candidate(rows, item / "model")
            except OSError:
                pass
        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                _add_model_candidate(rows, child)
                _add_model_candidate(rows, child / "model")
                # HF/local snapshots are often one directory deeper.
                try:
                    for grandchild in child.iterdir():
                        if grandchild.is_dir() and grandchild.name.lower() in {"model", "snapshot", "snapshots"}:
                            _add_model_candidate(rows, grandchild)
                            if grandchild.name.lower() == "snapshots":
                                for snap in grandchild.iterdir():
                                    if snap.is_dir():
                                        _add_model_candidate(rows, snap)
                except OSError:
                    pass
        except OSError:
            pass

    # Hugging Face cache, including a custom HF_HOME if the user's runtime has one.
    hf_roots: list[Path] = []
    for raw in (
        os.environ.get("HF_HOME"),
        Path.home() / ".cache" / "huggingface",
        Path(os.environ.get("LOCALAPPDATA") or "") / "huggingface" if os.environ.get("LOCALAPPDATA") else None,
    ):
        if not raw:
            continue
        try:
            h = Path(raw).expanduser().resolve()
        except Exception:
            continue
        if h not in hf_roots:
            hf_roots.append(h)
    repo_dir_name = "models--Qwen--Qwen3-TTS-12Hz-0.6B-Base"
    for hf in hf_roots:
        for repo in (hf / "hub" / repo_dir_name, hf / repo_dir_name):
            snaps = repo / "snapshots"
            if snaps.is_dir():
                try:
                    for snap in snaps.iterdir():
                        _add_model_candidate(rows, snap)
                except OSError:
                    pass
    return rows


def _find_qwen_native_model(resolved: dict[str, Any]) -> tuple[Path | None, list[Path]]:
    rows = _qwen_native_model_candidates(resolved)
    return next((p for p in rows if _qwen_native_model_valid(p)), None), rows


def _find_existing_q8_pack(resolved: dict[str, Any]) -> Path | None:
    rows: list[Path] = []
    for raw in (
        QWEN_Q8_DEFAULT_DIR,
        ROOT / "models" / "Qwen3-TTS-0.6B-Q8",
        ROOT / "models" / "Qwen3-TTS-12Hz-0.6B-Q8",
        Path(str(resolved.get("dubroom_root") or "")) / "models" / "Qwen3-TTS-0.6B-Q8" if resolved.get("dubroom_root") else None,
        Path(str(resolved.get("dubroom_root") or "")) / "data" / "models" / "Qwen3-TTS-0.6B-Q8" if resolved.get("dubroom_root") else None,
    ):
        _add_model_candidate(rows, raw)
    return next((p for p in rows if _qwen_q8_pack_valid(p)), None)


def _discover_qwen_voices(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    for folder in sorted(root.iterdir(), key=lambda x: x.stat().st_mtime if x.exists() else 0.0, reverse=True):
        if not folder.is_dir():
            continue
        meta = read_json(folder / "profile.json", {})
        # v1.7 saves both a high-fidelity ICL prompt and an x-vector fallback.
        # Keep compatibility with the older single qwen_prompt.pt file.
        icl_prompt = folder / "qwen_prompt_icl.pt"
        legacy_prompt = folder / "qwen_prompt.pt"
        if not icl_prompt.is_file() and legacy_prompt.is_file():
            icl_prompt = legacy_prompt
        xvec_prompt = folder / "qwen_prompt_xvec.pt"
        reference = folder / "reference.wav"
        ref_text = str(meta.get("ref_text") or "").strip()
        # Old Q8/KoboldCpp profiles contain reference.wav + ref_text but no .pt.
        # Surface them so v1.7 can migrate them once on first use.
        if not icl_prompt.is_file() and not (reference.is_file() and ref_text):
            continue
        rows.append({
            "id": str(meta.get("id") or folder.name),
            "name": str(meta.get("name") or folder.name),
            "engine": "qwen",
            "prompt_path": str(icl_prompt),
            "icl_prompt_path": str(icl_prompt),
            "xvec_prompt_path": str(xvec_prompt) if xvec_prompt.is_file() else "",
            "reference_path": str(reference),
            "profile_dir": str(folder),
            "ref_text": ref_text,
            "language": str(meta.get("language") or "fr"),
            "needs_prompt": not icl_prompt.is_file(),
            "clone_mode": ("Migration .pt requise" if not icl_prompt.is_file() else ("ICL + x-vector fallback" if xvec_prompt.is_file() else "ICL")),
        })
    return rows


def _latest_storyrecap_project(story_root: Path) -> Path:
    projects = story_root / "projects"
    if not projects.is_dir():
        return story_root
    rows: list[Path] = []
    try:
        for p in projects.iterdir():
            if p.is_dir() and ((p / "project.json").is_file() or (p / "tts").is_dir()):
                rows.append(p)
    except OSError:
        return story_root
    if not rows:
        return story_root
    rows.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    return rows[0]


def _storyrecap_roots(config: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    for raw in (
        os.environ.get("STORY_RECAP_ROOT"),
        config.get("storyrecap_root"),
        Path(str(config.get("storydub_root") or "")) / "StoryRecapper_Pipeline_Current"
        if config.get("storydub_root") else None,
        r"D:\manhwa studio\StoryDub\StoryRecapper_Pipeline_Current",
    ):
        if not raw:
            continue
        try:
            p = Path(raw).expanduser().resolve()
        except Exception:
            continue
        if p not in candidates:
            candidates.append(p)
    return candidates


def detect_from_storyrecapper(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reuse the detection path already used by Story Recapper patches.

    Preferred source of truth:
      simple_ui/script_studio_023.py::tts_capabilities(project)
      simple_ui/script_studio_023.py::_dubroom_root()

    Only if that module cannot be loaded do we fall back to the same validated
    DubRoom file layout. Nothing is downloaded and no model path is invented.
    """
    resolved = dict(config)
    diag: dict[str, Any] = {
        "source": "",
        "storyrecap_root": "",
        "project_probe": "",
        "runner": "",
        "model_root": "",
        "messages": [],
    }

    story_root: Path | None = None
    for candidate in _storyrecap_roots(config):
        if (candidate / "simple_ui" / "script_studio_023.py").is_file():
            story_root = candidate
            break
    if story_root:
        diag["storyrecap_root"] = str(story_root)
        simple_ui = story_root / "simple_ui"
        project_probe = _latest_storyrecap_project(story_root)
        diag["project_probe"] = str(project_probe)
        try:
            simple_text = str(simple_ui)
            if simple_text not in sys.path:
                sys.path.insert(0, simple_text)
            module = importlib.import_module("script_studio_023")
            caps = module.tts_capabilities(project_probe) if hasattr(module, "tts_capabilities") else {}
            if isinstance(caps, dict):
                py = str(caps.get("python") or "").strip()
                runner = str(caps.get("runner") or "").strip()
                model_root = str(caps.get("model_root") or "").strip()
                if py:
                    resolved["runtime_python"] = py
                if runner:
                    diag["runner"] = runner
                if model_root:
                    diag["model_root"] = model_root
                    mr = Path(model_root)
                    model_dir = mr / "model"
                    resolved["model_path"] = str(model_dir if model_dir.is_dir() else mr)
                diag["caps_ready"] = bool(caps.get("ready"))
                diag["caps_missing"] = list(caps.get("missing") or [])
            if hasattr(module, "_dubroom_root"):
                dubroom = Path(module._dubroom_root()).resolve()
                resolved["dubroom_root"] = str(dubroom)
                if not str(resolved.get("runtime_python") or ""):
                    resolved["runtime_python"] = str(dubroom / "data" / "environments" / "tts-omnivoice-hq" / "venv" / "Scripts" / "python.exe")
                if not diag["runner"]:
                    diag["runner"] = str(dubroom / "scripts" / "engines" / "run-tts.py")
                if not diag["model_root"]:
                    model_root = dubroom / "models" / "tts-omnivoice-hq"
                    diag["model_root"] = str(model_root)
                    resolved["model_path"] = str(model_root / "model")
            diag["source"] = "StoryRecapper · script_studio_023.tts_capabilities"
        except Exception as exc:
            diag["messages"].append(f"script_studio_023: {exc}")

    # Fallback uses the exact DubRoom layout already exercised by run-tts.py.
    if not diag["source"]:
        dubroom_candidates: list[Path] = []
        for raw in (
            resolved.get("dubroom_root"),
            os.environ.get("DUBROOM_ROOT"),
            r"D:\manhwa studio\anime_manga_manhua_dubbing_app",
        ):
            if not raw:
                continue
            try:
                p = Path(str(raw)).expanduser().resolve()
            except Exception:
                continue
            if p not in dubroom_candidates:
                dubroom_candidates.append(p)
        for dubroom in dubroom_candidates:
            runner = dubroom / "scripts" / "engines" / "run-tts.py"
            python = dubroom / "data" / "environments" / "tts-omnivoice-hq" / "venv" / "Scripts" / "python.exe"
            model_roots = [
                dubroom / "data" / "models" / "tts-omnivoice-hq",
                dubroom / "models" / "tts-omnivoice-hq",
            ]
            model_root = next((m for m in model_roots if (m / "model").is_dir()), None)
            if runner.is_file() and model_root is not None and python.is_file():
                resolved["dubroom_root"] = str(dubroom)
                resolved["runtime_python"] = str(python)
                resolved["model_path"] = str(model_root / "model")
                diag["runner"] = str(runner)
                diag["model_root"] = str(model_root)
                diag["source"] = "Dubroom · validated shared runtime"
                break

    # Reuse the Faster-Whisper Turbo runtime/model already validated by Story Recapper.
    dubroom_for_qc = _existing_dir(resolved.get("dubroom_root"))
    if dubroom_for_qc:
        qc_python = dubroom_for_qc / "data" / "environments" / "faster-whisper-turbo" / "venv" / "Scripts" / "python.exe"
        qc_models = [
            dubroom_for_qc / "models" / "faster-whisper-turbo" / "snapshot",
            dubroom_for_qc / "data" / "models" / "faster-whisper-turbo" / "snapshot",
        ]
        qc_model = next((x for x in qc_models if x.is_dir()), None)
        if qc_python.is_file():
            resolved["qc_runtime_python"] = str(qc_python)
        if qc_model is not None:
            resolved["qc_model_path"] = str(qc_model)
        diag["qc_runtime_python"] = str(qc_python)
        diag["qc_model_path"] = str(qc_model or qc_models[0])
        diag["qc_ready"] = bool(qc_python.is_file() and qc_model is not None)

    # Narrator discovery reuses the exact StoryDub profile store used in the patch
    # requests: StoryDub/data/narrator_voices/<voice_id>/{reference.wav,omnivoice_prompt.pt}.
    storydub_candidates: list[Path] = []
    for raw in (
        resolved.get("storydub_root"),
        story_root.parent if story_root else None,
        r"D:\manhwa studio\StoryDub",
    ):
        if not raw:
            continue
        try:
            p = Path(str(raw)).expanduser().resolve()
        except Exception:
            continue
        if p not in storydub_candidates:
            storydub_candidates.append(p)

    detected_voices: list[dict[str, Any]] = []
    for storydub in storydub_candidates:
        voice_root = storydub / "data" / "narrator_voices"
        rows: list[dict[str, Any]] = []
        for voice_id, name in STORYDUB_NARRATORS:
            folder = voice_root / voice_id
            prompt = folder / "omnivoice_prompt.pt"
            reference = folder / "reference.wav"
            rows.append({
                "id": voice_id,
                "name": name,
                "prompt_path": str(prompt),
                "reference_path": str(reference),
                "profile_dir": str(folder),
            })
        if any(Path(r["prompt_path"]).is_file() for r in rows):
            detected_voices = rows
            resolved["storydub_root"] = str(storydub)
            break

    if detected_voices:
        resolved["voices"] = detected_voices
        diag["voices_source"] = "StoryDub/data/narrator_voices · PATCH045/PATCH062 profiles"
    else:
        diag["voices_source"] = "config fallback"

    # Qwen3-TTS 0.6B Base is optional and strictly additive. Qwen often lives
    # in a different venv, so scan existing runtimes instead of assuming it is
    # inside OmniVoice's environment. Nothing is installed here.
    qwen_python, qwen_runtime_candidates = _find_python_with_module(resolved, "qwen_tts")
    if qwen_python is not None:
        resolved["qwen_runtime_python"] = str(qwen_python)
    download_python, _ = _find_python_with_module(resolved, "huggingface_hub")
    if download_python is not None:
        resolved["qwen_download_python"] = str(download_python)

    qwen_model, qwen_model_candidates = _find_qwen_native_model(resolved)
    qwen_q8_model = _find_existing_q8_pack(resolved)
    resolved["qwen_model_path"] = str(qwen_model or QWEN_MODEL_DEFAULT_DIR.resolve())
    resolved["qwen_model_repo"] = QWEN_MODEL_REPO
    resolved["qwen_voice_root"] = str(QWEN_VOICES_DIR.resolve())
    resolved["qwen_voices"] = _discover_qwen_voices(QWEN_VOICES_DIR)
    diag["qwen_runtime_python"] = str(qwen_python or "")
    diag["qwen_runtime_candidates"] = [str(x) for x in qwen_runtime_candidates[:20]]
    diag["qwen_download_python"] = str(download_python or "")
    diag["qwen_model_path"] = resolved["qwen_model_path"]
    diag["qwen_model_candidates"] = [str(x) for x in qwen_model_candidates[:40]]
    diag["qwen_q8_path"] = str(qwen_q8_model or "")
    diag["qwen_ready"] = bool(qwen_python is not None and qwen_model is not None and QWEN_WORKER_SCRIPT.is_file())

    defaults = dict(resolved.get("defaults") or {})
    if not any(v.get("id") == defaults.get("voice_id") for v in resolved.get("voices") or []):
        defaults["voice_id"] = STORYDUB_NARRATORS[0][0]
    defaults.setdefault("engine", "omnivoice")
    resolved["defaults"] = defaults
    return resolved, diag


class AppHandler(SimpleHTTPRequestHandler):
    """Static UI + tiny localhost JSON API.

    The browser UI talks to Python over ordinary HTTP on 127.0.0.1. This is
    deliberately independent from ``window.pywebview.api``: PyWebView is only
    the desktop shell, so changes in its JS bridge cannot break the application.
    """

    server_version = "NarratorStudio/1.8.0-qwen06-native-pt-autodetect"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    @property
    def app_api(self):
        api = getattr(self.server, "app_api", None)
        if api is None:
            raise RuntimeError("API locale pas encore initialisée")
        return api

    def _json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(max(0, min(length, 40_000_000))) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    def _audio_file(self, *, attachment: bool) -> None:
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(self.path).query)
        name = Path(str((query.get("filename") or [""])[0])).name
        path = HISTORY_DIR / name
        if not name or path.suffix.lower() != ".wav" or not path.is_file():
            self._json({"ok": False, "error": "Audio introuvable."}, 404)
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open("rb") as fh:
            shutil.copyfileobj(fh, self.wfile, length=1024 * 256)

    def do_GET(self) -> None:
        from urllib.parse import urlsplit
        route = urlsplit(self.path).path
        try:
            if route == "/api/bootstrap":
                return self._json(self.app_api.get_bootstrap())
            if route == "/api/job":
                return self._json(self.app_api.get_job())
            if route == "/api/history":
                return self._json(self.app_api.get_history())
            if route == "/api/qwen/status":
                return self._json(self.app_api.get_qwen_status())
            if route == "/api/audio":
                return self._audio_file(attachment=False)
            if route == "/api/download":
                return self._audio_file(attachment=True)
            return super().do_GET()
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)}, 500)

    def do_POST(self) -> None:
        from urllib.parse import urlsplit
        route = urlsplit(self.path).path
        body = self._body_json()
        try:
            if route == "/api/generate":
                return self._json(self.app_api.start_generate(body))
            if route == "/api/cancel":
                return self._json(self.app_api.cancel_generate())
            if route == "/api/delete":
                return self._json(self.app_api.delete_history(str(body.get("filename") or "")))
            if route == "/api/save":
                return self._json(self.app_api.save_audio(str(body.get("filename") or "")))
            if route == "/api/open-folder":
                return self._json(self.app_api.open_history_folder())
            if route == "/api/engine":
                return self._json(self.app_api.set_engine(str(body.get("engine") or "")))
            if route == "/api/qwen/clone":
                return self._json(self.app_api.create_qwen_clone(body))
            if route == "/api/qwen/download-model":
                return self._json(self.app_api.start_qwen_model_download())
            if route == "/api/qwen/runtime":
                return self._json(self.app_api.set_qwen_runtime(str(body.get("python_path") or "")))
            if route == "/api/qwen/model-path":
                return self._json(self.app_api.set_qwen_model_path(str(body.get("model_path") or "")))
            if route == "/api/qwen/rescan-runtime":
                return self._json(self.app_api.rescan_qwen_runtime())
            return self._json({"ok": False, "error": "Route API inconnue."}, 404)
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)}, 500)


class LocalServer:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AppHandler)
        self.server.app_api = None
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="NarratorStudio-HTTP")

    def bind_api(self, api) -> None:
        self.server.app_api = api

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class OmniWorker:
    def __init__(self, config: dict[str, Any], on_event=None):
        self.config = config
        self.on_event = on_event
        self.proc: subprocess.Popen | None = None
        self.lock = threading.RLock()
        self.reader_lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            python = Path(str(self.config.get("runtime_python") or ""))
            if not python.is_file():
                raise FileNotFoundError(f"OmniVoice Python introuvable: {python}")
            if not WORKER_SCRIPT.is_file():
                raise FileNotFoundError(f"Worker introuvable: {WORKER_SCRIPT}")
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["HF_HUB_OFFLINE"] = "1"
            env["TRANSFORMERS_OFFLINE"] = "1"
            self.proc = subprocess.Popen(
                [str(python), "-u", str(WORKER_SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(ROOT),
                env=env,
                **hidden_popen_kwargs(),
            )

    def stop(self):
        with self.lock:
            p = self.proc
            self.proc = None
        if not p:
            return
        try:
            if p.poll() is None and p.stdin:
                rid = uuid.uuid4().hex
                p.stdin.write(json.dumps({"cmd": "shutdown", "request_id": rid}) + "\n")
                p.stdin.flush()
                p.wait(timeout=2)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass

    def kill(self):
        with self.lock:
            p = self.proc
            self.proc = None
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.reader_lock:
            self.start()
            p = self.proc
            if not p or not p.stdin or not p.stdout:
                raise RuntimeError("Le worker OmniVoice n'a pas démarré")
            rid = str(payload.get("request_id") or uuid.uuid4().hex)
            payload["request_id"] = rid
            p.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            p.stdin.flush()

            recent_logs: list[str] = []
            while True:
                line = p.stdout.readline()
                if not line:
                    code = p.poll()
                    tail = " | ".join(recent_logs[-4:]).strip()
                    detail = f" · {tail}" if tail else ""
                    raise RuntimeError(f"OmniVoice worker arrêté (code {code}){detail}")
                line = line.strip()
                try:
                    ev = json.loads(line)
                except Exception:
                    if line:
                        recent_logs.append(line[-1500:])
                    if self.on_event:
                        self.on_event({"event": "log", "message": line[-1500:]})
                    continue
                if self.on_event:
                    self.on_event(ev)
                if str(ev.get("request_id") or "") != rid:
                    continue
                if ev.get("event") in {"completed", "ready", "error", "shutdown"}:
                    if ev.get("event") == "error" or ev.get("ok") is False:
                        raise RuntimeError(str(ev.get("error") or "Erreur OmniVoice"))
                    return ev


class QwenWorker:
    def __init__(self, config: dict[str, Any], on_event=None):
        self.config = config
        self.on_event = on_event
        self.proc: subprocess.Popen | None = None
        self.lock = threading.RLock()
        self.reader_lock = threading.Lock()

    def available(self) -> bool:
        return (
            Path(str(self.config.get("qwen_runtime_python") or "")).is_file()
            and _qwen_native_model_valid(Path(str(self.config.get("qwen_model_path") or "")))
            and QWEN_WORKER_SCRIPT.is_file()
        )

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            python = Path(str(self.config.get("qwen_runtime_python") or ""))
            if not python.is_file():
                raise FileNotFoundError(f"Qwen3-TTS Python introuvable: {python}")
            if not QWEN_WORKER_SCRIPT.is_file():
                raise FileNotFoundError(f"Worker Qwen introuvable: {QWEN_WORKER_SCRIPT}")
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["HF_HUB_OFFLINE"] = "1"
            env["TRANSFORMERS_OFFLINE"] = "1"
            self.proc = subprocess.Popen(
                [str(python), "-u", str(QWEN_WORKER_SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(ROOT),
                env=env,
                **hidden_popen_kwargs(),
            )

    def stop(self):
        with self.lock:
            p = self.proc
            self.proc = None
        if not p:
            return
        try:
            if p.poll() is None and p.stdin:
                rid = uuid.uuid4().hex
                p.stdin.write(json.dumps({"cmd": "shutdown", "request_id": rid}) + "\n")
                p.stdin.flush()
                p.wait(timeout=3)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass

    def kill(self):
        with self.lock:
            p = self.proc
            self.proc = None
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.reader_lock:
            self.start()
            p = self.proc
            if not p or not p.stdin or not p.stdout:
                raise RuntimeError("Le worker Qwen3-TTS n'a pas démarré")
            rid = str(payload.get("request_id") or uuid.uuid4().hex)
            payload["request_id"] = rid
            p.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            p.stdin.flush()
            while True:
                line = p.stdout.readline()
                if not line:
                    code = p.poll()
                    raise RuntimeError(f"Qwen3-TTS worker arrêté (code {code})")
                line = line.strip()
                try:
                    ev = json.loads(line)
                except Exception:
                    if self.on_event:
                        self.on_event({"event": "qwen_log", "message": line[-1200:]})
                    continue
                if self.on_event:
                    self.on_event(ev)
                if str(ev.get("request_id") or "") != rid:
                    continue
                if ev.get("event") in {"completed", "ready", "prompt_created", "error", "shutdown"}:
                    if ev.get("event") == "error" or ev.get("ok") is False:
                        raise RuntimeError(str(ev.get("error") or "Erreur Qwen3-TTS"))
                    return ev


class QCWorker:
    def __init__(self, config: dict[str, Any], on_event=None):
        self.config = config
        self.on_event = on_event
        self.proc: subprocess.Popen | None = None
        self.lock = threading.RLock()
        self.reader_lock = threading.Lock()

    def available(self) -> bool:
        return (
            Path(str(self.config.get("qc_runtime_python") or "")).is_file()
            and Path(str(self.config.get("qc_model_path") or "")).is_dir()
            and QC_WORKER_SCRIPT.is_file()
        )

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            python = Path(str(self.config.get("qc_runtime_python") or ""))
            if not python.is_file():
                raise FileNotFoundError(f"Faster-Whisper Python introuvable: {python}")
            if not QC_WORKER_SCRIPT.is_file():
                raise FileNotFoundError(f"Worker QC introuvable: {QC_WORKER_SCRIPT}")
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["HF_HUB_OFFLINE"] = "1"
            env["TRANSFORMERS_OFFLINE"] = "1"
            self.proc = subprocess.Popen(
                [str(python), "-u", str(QC_WORKER_SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(ROOT),
                env=env,
                **hidden_popen_kwargs(),
            )

    def stop(self):
        with self.lock:
            p = self.proc
            self.proc = None
        if not p:
            return
        try:
            if p.poll() is None and p.stdin:
                rid = uuid.uuid4().hex
                p.stdin.write(json.dumps({"cmd": "shutdown", "request_id": rid}) + "\n")
                p.stdin.flush()
                p.wait(timeout=3)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass

    def kill(self):
        with self.lock:
            p = self.proc
            self.proc = None
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.reader_lock:
            self.start()
            p = self.proc
            if not p or not p.stdin or not p.stdout:
                raise RuntimeError("Le worker Faster-Whisper QC n'a pas démarré")
            rid = str(payload.get("request_id") or uuid.uuid4().hex)
            payload["request_id"] = rid
            p.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            p.stdin.flush()
            while True:
                line = p.stdout.readline()
                if not line:
                    code = p.poll()
                    raise RuntimeError(f"Faster-Whisper QC arrêté (code {code})")
                line = line.strip()
                try:
                    ev = json.loads(line)
                except Exception:
                    if self.on_event:
                        self.on_event({"event": "qc_log", "message": line[-1000:]})
                    continue
                if self.on_event:
                    self.on_event(ev)
                if str(ev.get("request_id") or "") != rid:
                    continue
                if ev.get("event") in {"transcribed", "ready", "error", "shutdown"}:
                    if ev.get("event") == "error" or ev.get("ok") is False:
                        raise RuntimeError(str(ev.get("error") or "Erreur Faster-Whisper QC"))
                    return ev


class AppAPI:
    def __init__(self, server: LocalServer):
        self.server = server
        raw_config = read_json(CONFIG_PATH, {})
        self.config, self.detection = detect_from_storyrecapper(raw_config)
        self.window = None
        self.job_lock = threading.RLock()
        self.engine_lock = threading.RLock()
        self.qwen_download_lock = threading.RLock()
        self.qwen_download: dict[str, Any] = {"running": False, "progress": 0, "stage": "", "error": ""}
        self.job: dict[str, Any] = {
            "running": False,
            "stage": "Démarrage",
            "progress": 0,
            "error": "",
            "ready": False,
            "qc_ready": False,
            "cancel_requested": False,
        }
        self.omni_worker = OmniWorker(self.config, self._worker_event)
        self.qwen_worker = QwenWorker(self.config, self._worker_event)
        self.qc_worker = QCWorker(self.config, self._worker_event)
        requested_engine = str((self.config.get("defaults") or {}).get("engine") or "omnivoice").lower()
        self.active_engine = "qwen" if requested_engine == "qwen" else "omnivoice"
        self.worker = self.qwen_worker if self.active_engine == "qwen" else self.omni_worker
        self.qc_disabled_for_job = False
        self._warm_thread = threading.Thread(target=self._warmup, args=(self.active_engine,), daemon=True)
        self._warm_thread.start()

    def bind_window(self, window):
        self.window = window

    def _set_job(self, **kwargs):
        with self.job_lock:
            self.job.update(kwargs)

    def _cancelled(self) -> bool:
        with self.job_lock:
            return bool(self.job.get("cancel_requested"))

    @staticmethod
    def _normalize_engine(engine: Any) -> str:
        return "qwen" if str(engine or "").strip().lower() in {"qwen", "qwen3", "qwen3-tts"} else "omnivoice"

    def _switch_engine(self, engine: Any) -> str:
        engine = self._normalize_engine(engine)
        with self.engine_lock:
            if engine == self.active_engine:
                self.worker = self.qwen_worker if engine == "qwen" else self.omni_worker
                return engine
            if engine == "qwen":
                self.omni_worker.stop()
                self.worker = self.qwen_worker
            else:
                self.qwen_worker.stop()
                self.worker = self.omni_worker
            self.active_engine = engine
            return engine

    def _worker_event(self, ev: dict[str, Any]):
        name = str(ev.get("event") or "")
        if name in {"runtime", "qwen_runtime"}:
            self._set_job(ready=True, gpu=str(ev.get("gpu") or self.get_job().get("gpu") or "CUDA"))
        elif name == "qc_runtime":
            self._set_job(
                qc_ready=True,
                qc_device=str(ev.get("device") or ""),
                qc_compute_type=str(ev.get("compute_type") or ""),
            )
        elif name in {"log", "qc_log", "qwen_log"}:
            self._set_job(log=str(ev.get("message") or ""))

    def _warmup(self, engine: str | None = None):
        engine = self._switch_engine(engine or self.active_engine)
        try:
            if engine == "qwen":
                if not self.qwen_worker.available():
                    raise RuntimeError("Qwen3-TTS 0.6B n'est pas prêt : runtime ou poids local manquant.")
                self._set_job(stage="Chargement Qwen3-TTS 0.6B…", progress=0, error="", ready=False)
                result = self.qwen_worker.request({"cmd": "init", "model_path": self.config.get("qwen_model_path")})
                self._set_job(
                    ready=True,
                    stage="Qwen3-TTS 0.6B prêt · Auto-QC Faster-Whisper disponible",
                    model_load_seconds=result.get("load_seconds"),
                    gpu=result.get("gpu") or "CUDA",
                    qc_available=self.qc_worker.available(),
                    engine="qwen",
                )
            else:
                self._set_job(stage="Chargement OmniVoice…", progress=0, error="", ready=False)
                result = self.omni_worker.request({"cmd": "init", "model_path": self.config.get("model_path")})
                self._set_job(
                    ready=True,
                    stage="OmniVoice HQ prêt · Auto-QC au lancement audio",
                    model_load_seconds=result.get("load_seconds"),
                    gpu=result.get("gpu") or "CUDA",
                    qc_available=self.qc_worker.available(),
                    engine="omnivoice",
                )
        except Exception as exc:
            self._set_job(ready=False, stage="Runtime indisponible", error=str(exc), engine=engine)

    def set_engine(self, engine: str) -> dict[str, Any]:
        with self.job_lock:
            if self.job.get("running"):
                return {"ok": False, "error": "Impossible de changer de moteur pendant une génération."}
        engine = self._switch_engine(engine)
        self._set_job(stage=("Chargement Qwen3-TTS 0.6B…" if engine == "qwen" else "Chargement OmniVoice…"), error="", ready=False)
        threading.Thread(target=self._warmup, args=(engine,), daemon=True).start()
        return {"ok": True, "engine": engine}

    def _refresh_qwen_voices(self) -> list[dict[str, Any]]:
        rows = _discover_qwen_voices(QWEN_VOICES_DIR)
        self.config["qwen_voices"] = rows
        return rows

    def get_qwen_status(self) -> dict[str, Any]:
        voices = self._refresh_qwen_voices()
        with self.qwen_download_lock:
            download = dict(self.qwen_download)
        download_py = Path(str(self.config.get("qwen_download_python") or ""))
        return {
            "ok": True,
            "ready": self.qwen_worker.available(),
            "runtime_python": self.config.get("qwen_runtime_python"),
            "runtime_python_exists": Path(str(self.config.get("qwen_runtime_python") or "")).is_file(),
            "runtime_candidates": list(self.detection.get("qwen_runtime_candidates") or []),
            "download_python": str(download_py) if download_py.is_file() else "",
            "download_capable": bool(download_py.is_file() and _python_imports(download_py, "huggingface_hub")),
            "model_path": self.config.get("qwen_model_path"),
            "model_exists": _qwen_native_model_valid(Path(str(self.config.get("qwen_model_path") or ""))),
            "model_candidates": list(self.detection.get("qwen_model_candidates") or []),
            "q8_path": str(self.detection.get("qwen_q8_path") or ""),
            "q8_detected": bool(self.detection.get("qwen_q8_path")),
            "model_repo": QWEN_MODEL_REPO,
            "voices": voices,
            "download": download,
            "active": self.active_engine == "qwen",
        }

    def get_bootstrap(self) -> dict[str, Any]:
        voices = []
        for v in self.config.get("voices") or []:
            row = dict(v)
            row["engine"] = "omnivoice"
            row["prompt_exists"] = Path(str(row.get("prompt_path") or "")).is_file()
            row["reference_exists"] = Path(str(row.get("reference_path") or "")).is_file()
            voices.append(row)
        qwen_voices = []
        for v in self._refresh_qwen_voices():
            row = dict(v)
            row["prompt_exists"] = Path(str(row.get("prompt_path") or "")).is_file()
            row["reference_exists"] = Path(str(row.get("reference_path") or "")).is_file()
            qwen_voices.append(row)
        return {
            "ok": True,
            "version": "1.8.0-qwen06-native-pt-autodetect",
            "active_engine": self.active_engine,
            "voices": voices,
            "qwen_voices": qwen_voices,
            "defaults": self.config.get("defaults") or {},
            "runtime": {
                "python_exists": Path(str(self.config.get("runtime_python") or "")).is_file(),
                "model_exists": Path(str(self.config.get("model_path") or "")).is_dir(),
                "model_path": self.config.get("model_path"),
                "runtime_python": self.config.get("runtime_python"),
                "runner": self.detection.get("runner"),
                "detection_source": self.detection.get("source"),
                "storyrecap_root": self.detection.get("storyrecap_root"),
                "project_probe": self.detection.get("project_probe"),
                "caps_ready": self.detection.get("caps_ready"),
                "caps_missing": self.detection.get("caps_missing") or [],
                "messages": self.detection.get("messages") or [],
                "qc_python_exists": Path(str(self.config.get("qc_runtime_python") or "")).is_file(),
                "qc_model_exists": Path(str(self.config.get("qc_model_path") or "")).is_dir(),
                "qc_runtime_python": self.config.get("qc_runtime_python"),
                "qc_model_path": self.config.get("qc_model_path"),
                "qc_ready": self.qc_worker.available(),
                "qwen_python_exists": Path(str(self.config.get("qwen_runtime_python") or "")).is_file(),
                "qwen_model_exists": _qwen_native_model_valid(Path(str(self.config.get("qwen_model_path") or ""))),
                "qwen_q8_detected": bool(self.detection.get("qwen_q8_path")),
                "qwen_q8_path": str(self.detection.get("qwen_q8_path") or ""),
                "qwen_runtime_python": self.config.get("qwen_runtime_python"),
                "qwen_download_python": self.config.get("qwen_download_python"),
                "qwen_download_capable": bool(Path(str(self.config.get("qwen_download_python") or "")).is_file()),
                "qwen_model_path": self.config.get("qwen_model_path"),
                "qwen_model_repo": QWEN_MODEL_REPO,
                "qwen_ready": self.qwen_worker.available(),
            },
            "voice_detection_source": self.detection.get("voices_source"),
            "job": self.get_job(),
            "history": self.get_history(),
            "qwen_download": self.get_qwen_status().get("download"),
            "base_url": f"http://127.0.0.1:{self.server.port}",
        }

    def get_job(self) -> dict[str, Any]:
        with self.job_lock:
            row = dict(self.job)
        row["active_engine"] = self.active_engine
        return row

    def set_qwen_runtime(self, python_path: str) -> dict[str, Any]:
        with self.job_lock:
            if self.job.get("running"):
                return {"ok": False, "error": "Attends la fin de la génération avant de changer le runtime Qwen."}
        try:
            py = Path(str(python_path or "")).expanduser().resolve()
        except Exception:
            return {"ok": False, "error": "Chemin Python Qwen invalide."}
        if not py.is_file():
            return {"ok": False, "error": f"python.exe introuvable : {py}"}
        _PY_IMPORT_CACHE.pop((str(py).lower(), "qwen_tts"), None)
        if not _python_imports(py, "qwen_tts"):
            return {"ok": False, "error": f"Ce Python existe mais n'importe pas qwen_tts : {py}"}
        self.qwen_worker.stop()
        self.config["qwen_runtime_python"] = str(py)
        if _python_imports(py, "huggingface_hub"):
            self.config["qwen_download_python"] = str(py)
        current = read_json(CONFIG_PATH, {})
        if not isinstance(current, dict):
            current = {}
        current["qwen_runtime_python"] = str(py)
        if self.config.get("qwen_download_python"):
            current["qwen_download_python"] = self.config.get("qwen_download_python")
        write_json(CONFIG_PATH, current)
        return {"ok": True, "runtime_python": str(py), "ready": self.qwen_worker.available()}

    def rescan_qwen_runtime(self) -> dict[str, Any]:
        # Re-scan BOTH the Python runtime and all bounded native model locations.
        # This fixes installs where the weights already live in DubRoom/data/models
        # or the Hugging Face cache under a different folder name.
        py, candidates = _find_python_with_module(self.config, "qwen_tts")
        self.detection["qwen_runtime_candidates"] = [str(x) for x in candidates[:20]]
        if py is not None:
            self.config["qwen_runtime_python"] = str(py)
        dl_py, _ = _find_python_with_module(self.config, "huggingface_hub")
        if dl_py is not None:
            self.config["qwen_download_python"] = str(dl_py)

        model, model_candidates = _find_qwen_native_model(self.config)
        q8 = _find_existing_q8_pack(self.config)
        self.detection["qwen_model_candidates"] = [str(x) for x in model_candidates[:40]]
        self.detection["qwen_q8_path"] = str(q8 or "")
        if model is not None:
            self.qwen_worker.stop()
            self.config["qwen_model_path"] = str(model)

        current = read_json(CONFIG_PATH, {})
        if not isinstance(current, dict):
            current = {}
        if py is not None:
            current["qwen_runtime_python"] = str(py)
        if dl_py is not None:
            current["qwen_download_python"] = str(dl_py)
        if model is not None:
            current["qwen_model_path"] = str(model)
        write_json(CONFIG_PATH, current)
        return self.get_qwen_status()

    def set_qwen_model_path(self, model_path: str) -> dict[str, Any]:
        with self.job_lock:
            if self.job.get("running"):
                return {"ok": False, "error": "Attends la fin de la génération avant de changer le modèle Qwen."}
        try:
            path = Path(str(model_path or "")).expanduser().resolve()
        except Exception:
            return {"ok": False, "error": "Chemin du modèle Qwen invalide."}
        if _qwen_q8_pack_valid(path):
            self.detection["qwen_q8_path"] = str(path)
            return {
                "ok": False,
                "error": "Ce dossier contient le pack GGUF Q8 KoboldCpp. Il fonctionne avec le backend Q8, mais ne peut pas créer les profils .pt natifs. Choisis le dossier Qwen3-TTS-12Hz-0.6B-Base contenant config.json + model.safetensors."
            }
        if not _qwen_native_model_valid(path):
            return {
                "ok": False,
                "error": "Snapshot Qwen natif incomplet. Le dossier doit contenir config.json, model.safetensors et le tokenizer audio du modèle Base."
            }
        self.qwen_worker.stop()
        self.config["qwen_model_path"] = str(path)
        self.detection["qwen_model_path"] = str(path)
        current = read_json(CONFIG_PATH, {})
        if not isinstance(current, dict):
            current = {}
        current["qwen_model_path"] = str(path)
        write_json(CONFIG_PATH, current)
        return self.get_qwen_status()

    def start_qwen_model_download(self) -> dict[str, Any]:
        target = Path(str(self.config.get("qwen_model_path") or QWEN_MODEL_DEFAULT_DIR)).expanduser().resolve()
        if (target / "config.json").is_file() and (target / "model.safetensors").is_file():
            return {"ok": True, "already_present": True, "path": str(target)}
        py = Path(str(self.config.get("qwen_download_python") or self.config.get("qwen_runtime_python") or ""))
        if not py.is_file() or not _python_imports(py, "huggingface_hub"):
            dl_py, _ = _find_python_with_module(self.config, "huggingface_hub")
            if dl_py is not None:
                py = dl_py
                self.config["qwen_download_python"] = str(dl_py)
        if not py.is_file():
            return {"ok": False, "error": "Aucun Python existant avec huggingface_hub n'a été détecté. Aucun package ne sera installé automatiquement."}
        with self.qwen_download_lock:
            if self.qwen_download.get("running"):
                return {"ok": True, "running": True, "path": str(target)}
            self.qwen_download = {
                "running": True,
                "progress": 0.5,
                "stage": "Connexion à Hugging Face · Qwen3-TTS 0.6B Base…",
                "error": "",
                "path": str(target),
                "bytes_done": 0,
                "total_bytes": 2_520_000_000,
                "speed_bps": 0,
                "eta_seconds": None,
            }

        def directory_size(root: Path) -> int:
            total = 0
            try:
                for f in root.rglob("*"):
                    if f.is_file() and ".cache" not in f.parts:
                        try:
                            total += f.stat().st_size
                        except OSError:
                            pass
            except OSError:
                pass
            return total

        def run_download():
            monitor_stop = threading.Event()
            started = time.perf_counter()
            expected = 2_520_000_000
            def monitor():
                last_b = 0
                last_t = started
                while not monitor_stop.wait(0.6):
                    done = directory_size(target)
                    now = time.perf_counter()
                    dt = max(0.05, now - last_t)
                    speed = max(0.0, (done - last_b) / dt)
                    last_b, last_t = done, now
                    pct = min(98.5, max(0.5, done * 100.0 / expected))
                    eta = ((expected - done) / speed) if speed > 1024 else None
                    with self.qwen_download_lock:
                        self.qwen_download.update({
                            "progress": pct,
                            "stage": "Téléchargement des poids officiels Qwen3-TTS 0.6B Base…",
                            "bytes_done": done,
                            "total_bytes": expected,
                            "speed_bps": speed,
                            "eta_seconds": eta,
                        })
            mon = threading.Thread(target=monitor, daemon=True, name="Qwen06DownloadProgress")
            mon.start()
            try:
                target.mkdir(parents=True, exist_ok=True)
                code = (
                    "from huggingface_hub import snapshot_download; "
                    f"snapshot_download(repo_id={QWEN_MODEL_REPO!r}, local_dir={str(target)!r})"
                )
                env = os.environ.copy()
                env.pop("HF_HUB_OFFLINE", None)
                env.pop("TRANSFORMERS_OFFLINE", None)
                env["HF_HUB_DISABLE_PROGRESS_BARS"] = "0"
                log_path = WORK_DIR / "qwen06_native_download.log"
                with log_path.open("w", encoding="utf-8", errors="replace") as log_fh:
                    proc = subprocess.run(
                        [str(py), "-c", code],
                        cwd=str(ROOT),
                        env=env,
                        text=True,
                        stdout=log_fh,
                        stderr=subprocess.STDOUT,
                        **hidden_popen_kwargs(),
                    )
                if proc.returncode != 0:
                    try:
                        details = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                    except Exception:
                        details = "Téléchargement Hugging Face échoué"
                    raise RuntimeError(details or "Téléchargement Hugging Face échoué")
                required = [target / "config.json", target / "model.safetensors"]
                if not all(x.is_file() for x in required):
                    raise RuntimeError("Le snapshot Qwen est incomplet : config.json ou model.safetensors manque.")
                # The 12 Hz speech tokenizer is shipped as a subfolder in this checkpoint.
                speech_dir = target / "speech_tokenizer"
                if not speech_dir.is_dir():
                    raise RuntimeError("Le snapshot Qwen est incomplet : speech_tokenizer est absent.")
                self.config["qwen_model_path"] = str(target)
                self.detection["qwen_model_path"] = str(target)
                self.detection["qwen_model_candidates"] = [str(target)] + [x for x in self.detection.get("qwen_model_candidates", []) if x != str(target)]
                current = read_json(CONFIG_PATH, {})
                if not isinstance(current, dict):
                    current = {}
                current["qwen_model_path"] = str(target)
                if self.config.get("qwen_runtime_python"):
                    current["qwen_runtime_python"] = self.config.get("qwen_runtime_python")
                current["qwen_download_python"] = str(py)
                write_json(CONFIG_PATH, current)
                done = directory_size(target)
                with self.qwen_download_lock:
                    self.qwen_download = {
                        "running": False,
                        "progress": 100,
                        "stage": "Qwen3-TTS 0.6B Base officiel prêt",
                        "error": "",
                        "path": str(target),
                        "bytes_done": done,
                        "total_bytes": done,
                        "speed_bps": 0,
                        "eta_seconds": 0,
                    }
            except Exception as exc:
                with self.qwen_download_lock:
                    self.qwen_download.update({
                        "running": False,
                        "stage": "Échec du téléchargement",
                        "error": str(exc),
                        "speed_bps": 0,
                        "eta_seconds": None,
                    })
            finally:
                monitor_stop.set()

        threading.Thread(target=run_download, daemon=True, name="Qwen06NativeWeightsDownload").start()
        return {"ok": True, "running": True, "path": str(target)}

    @staticmethod
    def _decode_uploaded_audio(payload: dict[str, Any], folder: Path) -> Path:
        name = Path(str(payload.get("filename") or "reference.wav")).name
        ext = Path(name).suffix.lower()
        if ext not in {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac"}:
            ext = ".wav"
        encoded = str(payload.get("audio_base64") or "")
        if "," in encoded and encoded.lstrip().startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        if not encoded:
            raise ValueError("Choisis un fichier audio de référence.")
        try:
            raw = base64.b64decode(encoded, validate=False)
        except Exception as exc:
            raise ValueError(f"Audio de référence invalide: {exc}") from exc
        if not raw or len(raw) > 28_000_000:
            raise ValueError("Référence audio vide ou trop lourde (28 Mo max).")
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / f"upload{ext}"
        source.write_bytes(raw)
        return source

    @staticmethod
    def _reference_to_wav(source: Path, target: Path) -> None:
        ffmpeg = shutil.which("ffmpeg")
        target.parent.mkdir(parents=True, exist_ok=True)
        if ffmpeg:
            proc = subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(target)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **hidden_popen_kwargs(),
            )
            if proc.returncode == 0 and target.is_file():
                return
            raise RuntimeError((proc.stderr or "ffmpeg n'a pas pu convertir la référence audio")[-1800:])
        if source.suffix.lower() == ".wav":
            shutil.copy2(source, target)
            return
        raise RuntimeError("ffmpeg est introuvable. Utilise une référence WAV ou rends ffmpeg accessible dans PATH.")

    def create_qwen_clone(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.qc_worker.available():
            return {"ok": False, "error": "Faster-Whisper Turbo local est introuvable. Aucun autre ASR ne sera installé."}
        if not Path(str(self.config.get("qwen_runtime_python") or "")).is_file():
            return {"ok": False, "error": "Runtime Qwen introuvable. Le patch ne réinstalle aucune dépendance."}
        qroot = Path(str(self.config.get("qwen_model_path") or ""))
        if not (qroot / "config.json").is_file() or not (qroot / "model.safetensors").is_file():
            return {"ok": False, "error": "Poids officiels Qwen3-TTS 0.6B Base absents. Utilise le bouton de téléchargement dans l'onglet Qwen."}

        name = " ".join(str(payload.get("name") or "").split()).strip()[:80]
        if not name:
            raise ValueError("Donne un nom à la voix Qwen.")
        language = str(payload.get("language") or "fr").lower().split("-", 1)[0]
        tmp_dir = WORK_DIR / f"qwen_clone_{uuid.uuid4().hex}"
        profile_dir: Path | None = None
        try:
            source = self._decode_uploaded_audio(payload, tmp_dir)
            normalized = tmp_dir / "reference.wav"
            self._reference_to_wav(source, normalized)

            # 8 GB VRAM: use the GPU sequentially. Whisper transcribes first, then
            # is released before Qwen extracts and saves the reusable voice prompts.
            self.omni_worker.stop()
            self.qwen_worker.stop()
            self._set_job(stage="Qwen clone · transcription Faster-Whisper Turbo…", error="")
            ref_text_override = " ".join(str(payload.get("ref_text") or "").split()).strip()
            if ref_text_override:
                ref_text = ref_text_override
            else:
                qc = self.qc_worker.request({
                    "cmd": "transcribe",
                    "model_path": self.config.get("qc_model_path"),
                    "audio_path": str(normalized),
                    "language": language,
                })
                ref_text = " ".join(str(qc.get("text") or "").split()).strip()
            self.qc_worker.stop()
            if len(ref_text) < 3:
                raise RuntimeError("Faster-Whisper n'a pas obtenu de transcription exploitable de la référence.")

            voice_id = f"qwen_{uuid.uuid4().hex[:16]}"
            profile_dir = QWEN_VOICES_DIR / voice_id
            profile_dir.mkdir(parents=True, exist_ok=False)
            reference_path = profile_dir / "reference.wav"
            icl_prompt_path = profile_dir / "qwen_prompt_icl.pt"
            xvec_prompt_path = profile_dir / "qwen_prompt_xvec.pt"
            shutil.copy2(normalized, reference_path)

            self._switch_engine("qwen")
            self._set_job(stage="Qwen clone · extraction ICL + x-vector vers .pt…", error="", ready=False)
            result = self.qwen_worker.request({
                "cmd": "create_prompts",
                "model_path": self.config.get("qwen_model_path"),
                "ref_audio_path": str(reference_path),
                "ref_text": ref_text,
                "icl_prompt_path": str(icl_prompt_path),
                "xvec_prompt_path": str(xvec_prompt_path),
            })
            if not icl_prompt_path.is_file():
                raise RuntimeError("Qwen n'a pas créé le prompt ICL .pt.")
            meta = {
                "id": voice_id,
                "name": name,
                "engine": "qwen",
                "language": language,
                "ref_text": ref_text,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "model": QWEN_MODEL_REPO,
                "clone_mode": "ICL + x-vector fallback",
                "icl_prompt": icl_prompt_path.name,
                "xvec_prompt": xvec_prompt_path.name if xvec_prompt_path.is_file() else "",
            }
            write_json(profile_dir / "profile.json", meta)
            voices = self._refresh_qwen_voices()
            voice = next((v for v in voices if v.get("id") == voice_id), meta)
            self._set_job(ready=True, stage="Voix Qwen enregistrée en .pt · prête à générer", engine="qwen", error="", gpu=result.get("gpu") or self.get_job().get("gpu") or "CUDA")
            return {
                "ok": True,
                "voice": voice,
                "transcript": ref_text,
                "icl_prompt_path": str(icl_prompt_path),
                "xvec_prompt_path": str(xvec_prompt_path) if xvec_prompt_path.is_file() else "",
                "elapsed": result.get("elapsed"),
            }
        except Exception:
            if profile_dir is not None and profile_dir.is_dir() and not (profile_dir / "qwen_prompt_icl.pt").is_file():
                shutil.rmtree(profile_dir, ignore_errors=True)
            raise
        finally:
            self.qc_worker.stop()
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def start_generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.job_lock:
            if self.job.get("running"):
                return {"ok": False, "error": "Une génération est déjà en cours."}
            self.job.update({
                "running": True,
                "progress": 0,
                "stage": "Préparation Auto-QC…",
                "error": "",
                "result": None,
                "cancel_requested": False,
                "retries": 0,
                "rescued_chunks": 0,
                "qc_scores": [],
            })
        thread = threading.Thread(target=self._generate_thread, args=(dict(payload or {}),), daemon=True)
        thread.start()
        return {"ok": True}

    @staticmethod
    def _merge_wavs(paths: list[Path], output: Path, gap_ms: int) -> float:
        if not paths:
            raise RuntimeError("Aucun chunk audio validé à assembler.")
        params = None
        chunks: list[bytes] = []
        sample_rate = 0
        channels = 1
        sample_width = 2
        for path in paths:
            with wave.open(str(path), "rb") as wf:
                current = (wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getcomptype())
                if current[1] != 2 or current[0] != 1:
                    raise RuntimeError(f"Format WAV inattendu pour {path.name}: {current}")
                if params is None:
                    params = current
                    channels, sample_width, sample_rate, _ = current
                elif current != params:
                    raise RuntimeError(f"Formats WAV incompatibles: {path.name}")
                chunks.append(wf.readframes(wf.getnframes()))
        gap_frames = max(0, int(sample_rate * max(0, min(500, gap_ms)) / 1000.0))
        silence = b"\x00" * gap_frames * channels * sample_width
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = output.with_name(output.stem + ".tmp.wav")
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            for i, data in enumerate(chunks):
                if i and silence:
                    wf.writeframesraw(silence)
                wf.writeframesraw(data)
        os.replace(tmp, output)
        with wave.open(str(output), "rb") as wf:
            return wf.getnframes() / float(wf.getframerate())

    def _qc_transcribe(self, wav: Path, language: str) -> dict[str, Any] | None:
        if self.qc_disabled_for_job or not self.qc_worker.available():
            return None
        return self.qc_worker.request({
            "cmd": "transcribe",
            "model_path": self.config.get("qc_model_path"),
            "audio_path": str(wav),
            "language": language,
        })

    def _attempt_steps(self, base_steps: int) -> list[int]:
        base = max(8, min(64, int(base_steps)))
        rows = [base, base, max(base, 20), max(base, 32)]
        out: list[int] = []
        for value in rows:
            if value not in out or value == base and len(out) < 2:
                out.append(value)
        return out

    def _render_piece(
        self,
        *,
        text: str,
        voice_id: str,
        voice: dict[str, Any],
        speed: float,
        steps: int,
        language: str,
        work_dir: Path,
        label: str,
        allow_rescue_split: bool = True,
    ) -> tuple[list[Path], list[dict[str, Any]]]:
        if self._cancelled():
            raise RuntimeError("Génération annulée")

        attempts = self._attempt_steps(steps)
        threshold = qc_threshold(text)
        soft_floor = qc_soft_floor(text)
        best: tuple[float, Path, dict[str, Any]] | None = None
        attempt_reports: list[dict[str, Any]] = []

        for attempt_no, attempt_steps in enumerate(attempts, start=1):
            if self._cancelled():
                raise RuntimeError("Génération annulée")
            seed = random.SystemRandom().randint(1, 2_000_000_000)
            wav = work_dir / f"{label}_try{attempt_no}_{attempt_steps}s.wav"
            qwen_temperature = None
            qwen_prompt_mode = None
            qwen_rp = None
            if self.active_engine == "qwen":
                # Attempt 1 uses the official checkpoint defaults. Attempts 2/3
                # make tiny sampling changes; attempt 4 switches to the saved
                # x-vector prompt if available, which can rescue rare ICL leakage.
                schedule = (
                    (0.90, 1.05, "icl"),
                    (0.85, 1.05, "icl"),
                    (0.95, 1.08, "icl"),
                    (0.90, 1.10, "xvec"),
                )
                qwen_temperature, qwen_rp, qwen_prompt_mode = schedule[min(attempt_no - 1, 3)]
                prompt_path = str(voice.get("icl_prompt_path") or voice.get("prompt_path") or "")
                if qwen_prompt_mode == "xvec" and Path(str(voice.get("xvec_prompt_path") or "")).is_file():
                    prompt_path = str(voice.get("xvec_prompt_path"))
                else:
                    qwen_prompt_mode = "icl"
                self._set_job(stage=f"{label} · Qwen {qwen_prompt_mode.upper()} essai {attempt_no}/{len(attempts)} · T={qwen_temperature:.2f}")
                render = self.qwen_worker.request({
                    "cmd": "generate_chunk",
                    "model_path": self.config.get("qwen_model_path"),
                    "prompt_path": prompt_path,
                    "output_path": str(wav),
                    "text": text,
                    "language": language,
                    "seed": seed,
                    "temperature": qwen_temperature,
                    "repetition_penalty": qwen_rp,
                    "max_new_tokens": 2048,
                })
                sanity = wav_sanity(wav, text, 1.0)
            else:
                self._set_job(stage=f"{label} · génération {attempt_no}/{len(attempts)} · {attempt_steps} steps")
                render = self.omni_worker.request({
                    "cmd": "generate_chunk",
                    "model_path": self.config.get("model_path"),
                    "voice_id": voice_id,
                    "prompt_path": voice.get("prompt_path"),
                    "output_path": str(wav),
                    "text": text,
                    "language": language,
                    "speed": speed,
                    "steps": attempt_steps,
                    "seed": seed,
                    "pad_duration": 0.12,
                    "fade_duration": 0.002,
                })
                sanity = wav_sanity(wav, text, speed)
            heard = ""
            sim = {"score": 1.0, "char_ratio": 1.0, "word_f1": 1.0}
            qc_mode = "acoustic"
            qc_error = ""
            if not self.qc_disabled_for_job and self.qc_worker.available() and sanity.get("ok"):
                self._set_job(stage=f"{label} · vérification prononciation…")
                try:
                    qc = self._qc_transcribe(wav, language)
                    if qc:
                        heard = str(qc.get("text") or "")
                        sim = text_similarity(text, heard)
                        qc_mode = f"whisper-{qc.get('device') or 'auto'}"
                except Exception as exc:
                    qc_error = str(exc)
                    # Do not destroy a usable render just because the QC model has
                    # an environment issue. Acoustic sanity remains active.
                    self.qc_worker.kill()
                    self.qc_disabled_for_job = True
                    qc_mode = "acoustic-fallback"

            score = float(sim.get("score") or 0.0) if sanity.get("ok") else 0.0
            accepted = bool(sanity.get("ok")) and (score >= threshold if qc_mode.startswith("whisper") else True)
            report = {
                "text": text,
                "heard": heard,
                "score": round(score, 4),
                "threshold": threshold,
                "steps": attempt_steps if self.active_engine == "omnivoice" else None,
                "temperature": qwen_temperature,
                "repetition_penalty": qwen_rp,
                "qwen_prompt_mode": qwen_prompt_mode,
                "engine": self.active_engine,
                "seed": seed,
                "attempt": attempt_no,
                "accepted": accepted,
                "sanity": sanity,
                "qc_mode": qc_mode,
                "qc_error": qc_error,
                "duration": render.get("duration"),
            }
            attempt_reports.append(report)
            self._set_job(
                stage=(f"{label} · QC {round(score*100)}%" if qc_mode.startswith("whisper") else f"{label} · contrôle audio OK"),
                last_qc=report,
            )
            if sanity.get("ok") and (best is None or score > best[0]):
                best = (score, wav, report)
            if accepted:
                return [wav], attempt_reports
            self._set_job(retries=int(self.get_job().get("retries") or 0) + 1)

        # Repeated failure: split the exact troublesome clause more aggressively.
        if allow_rescue_split:
            smaller = split_text_aggressive(text, max_chars=82, max_words=18)
            if len(smaller) > 1:
                self._set_job(
                    stage=f"{label} · rescue: découpage plus fin",
                    rescued_chunks=int(self.get_job().get("rescued_chunks") or 0) + 1,
                )
                accepted_paths: list[Path] = []
                reports = list(attempt_reports)
                for sub_i, sub in enumerate(smaller, start=1):
                    paths, sub_reports = self._render_piece(
                        text=sub,
                        voice_id=voice_id,
                        voice=voice,
                        speed=speed,
                        steps=max(steps, 16),
                        language=language,
                        work_dir=work_dir,
                        label=f"{label}.{sub_i}",
                        allow_rescue_split=False,
                    )
                    accepted_paths.extend(paths)
                    reports.extend(sub_reports)
                return accepted_paths, reports

        # Last-resort spoken-only rewrite for a tiny set of repeatedly proven
        # unstable constructions. The saved script remains untouched.
        safe_variant = speech_safe_variant(text)
        if safe_variant != text:
            self._set_job(stage=f"{label} · rescue: formulation TTS sûre")
            paths, safe_reports = self._render_piece(
                text=safe_variant,
                voice_id=voice_id,
                voice=voice,
                speed=speed,
                steps=max(steps, 20),
                language=language,
                work_dir=work_dir,
                label=f"{label}.safe",
                allow_rescue_split=False,
            )
            for row in safe_reports:
                row["source_text"] = text
                row["spoken_variant"] = safe_variant
            return paths, attempt_reports + safe_reports

        # A small Whisper mismatch can be a name/transcription issue. Accept only
        # if the audio itself is sane and the best score stays above a conservative
        # soft floor. Truly bizarre/non-speech output is rejected.
        if best is not None and best[0] >= soft_floor:
            best[2]["accepted"] = True
            best[2]["soft_accept"] = True
            return [best[1]], attempt_reports

        best_score = round((best[0] if best else 0.0) * 100)
        raise RuntimeError(
            f"Auto-QC a rejeté un passage après plusieurs essais ({best_score}%): {text[:140]}"
        )

    def _generate_thread(self, payload: dict[str, Any]):
        started_all = time.perf_counter()
        work_dir: Path | None = None
        try:
            text = clean_text(str(payload.get("text") or ""))
            if not text:
                raise ValueError("Colle d'abord un texte à lire.")
            if len(text) > 50000:
                raise ValueError("Texte trop long pour une seule génération (50 000 caractères max).")

            engine = self._normalize_engine(payload.get("engine") or self.active_engine)
            self._switch_engine(engine)
            if engine == "qwen":
                if not self.qwen_worker.available():
                    raise RuntimeError("Qwen3-TTS 0.6B n'est pas prêt : poids ou runtime local manquant.")
                self._set_job(stage="Chargement Qwen3-TTS 0.6B…", engine="qwen", ready=False)
                init = self.qwen_worker.request({"cmd": "init", "model_path": self.config.get("qwen_model_path")})
                self._set_job(ready=True, gpu=init.get("gpu") or "CUDA")
                voices = {str(v.get("id")): v for v in self._refresh_qwen_voices()}
                voice_id = str(payload.get("voice_id") or "")
                voice = voices.get(voice_id)
                if not voice:
                    raise ValueError("Choisis d'abord une voix clonée Qwen.")
                prompt_now = Path(str(voice.get("icl_prompt_path") or voice.get("prompt_path") or ""))
                if not prompt_now.is_file():
                    # One-time migration of profiles created by the older Q8 backend.
                    reference = Path(str(voice.get("reference_path") or ""))
                    ref_text = str(voice.get("ref_text") or "").strip()
                    profile_dir = Path(str(voice.get("profile_dir") or ""))
                    if not reference.is_file() or not ref_text or not profile_dir.is_dir():
                        raise FileNotFoundError("Prompt Qwen ICL .pt introuvable et ancien profil non migrable. Recrée cette voix.")
                    icl = profile_dir / "qwen_prompt_icl.pt"
                    xvec = profile_dir / "qwen_prompt_xvec.pt"
                    self._set_job(stage="Migration unique de l'ancien clone Q8 vers profils .pt…", engine="qwen", ready=False)
                    self.qwen_worker.request({
                        "cmd": "create_prompts",
                        "model_path": self.config.get("qwen_model_path"),
                        "ref_audio_path": str(reference),
                        "ref_text": ref_text,
                        "icl_prompt_path": str(icl),
                        "xvec_prompt_path": str(xvec),
                    })
                    voice["prompt_path"] = str(icl)
                    voice["icl_prompt_path"] = str(icl)
                    voice["xvec_prompt_path"] = str(xvec) if xvec.is_file() else ""
                    meta = read_json(profile_dir / "profile.json", {})
                    if not isinstance(meta, dict):
                        meta = {}
                    meta["model"] = QWEN_MODEL_REPO
                    meta["clone_mode"] = "ICL + x-vector fallback"
                    meta["icl_prompt"] = icl.name
                    meta["xvec_prompt"] = xvec.name if xvec.is_file() else ""
                    write_json(profile_dir / "profile.json", meta)
                    self._refresh_qwen_voices()
                    self._set_job(stage="Migration .pt terminée · génération…", engine="qwen", ready=True)
            else:
                self._set_job(stage="Chargement OmniVoice…", engine="omnivoice", ready=False)
                init = self.omni_worker.request({"cmd": "init", "model_path": self.config.get("model_path")})
                self._set_job(ready=True, gpu=init.get("gpu") or "CUDA")
                voice_id = str(payload.get("voice_id") or (self.config.get("defaults") or {}).get("voice_id") or "")
                voices = {str(v.get("id")): v for v in self.config.get("voices") or []}
                voice = voices.get(voice_id)
                if not voice:
                    raise ValueError("Voix narrateur OmniVoice inconnue.")
                if not Path(str(voice.get("prompt_path") or "")).is_file():
                    raise FileNotFoundError("Prompt OmniVoice du narrateur introuvable.")

            speed = 1.0 if engine == "qwen" else max(0.70, min(1.35, float(payload.get("speed") or 1.0)))
            steps = max(8, min(64, int(payload.get("steps") or 12)))
            language = str(payload.get("language") or "en").lower().split("-", 1)[0]
            defaults = self.config.get("defaults") or {}
            max_chars = int(defaults.get("qwen_max_chunk_chars") or 150) if engine == "qwen" else int(defaults.get("max_chunk_chars") or 140)
            max_words = int(defaults.get("qwen_max_chunk_words") or 30) if engine == "qwen" else int(defaults.get("max_chunk_words") or 30)
            gap_ms = int(defaults.get("gap_ms") or 55)
            chunks = split_text_smart(text, max_chars=max_chars, max_words=max_words)
            if not chunks:
                raise RuntimeError("Aucun passage TTS générable.")

            now = time.strftime("%Y%m%d_%H%M%S")
            if engine == "qwen":
                short_voice = "qwen"
            else:
                short_voice = "n1" if voice_id == "voice_ee40c6612ca1235e0c7a" else ("n2" if voice_id == "voice_73c9c20e2de552c1815d" else "voice")
            filename = f"{now}_{short_voice}_{uuid.uuid4().hex[:6]}.wav"
            output = HISTORY_DIR / filename
            work_dir = WORK_DIR / uuid.uuid4().hex
            work_dir.mkdir(parents=True, exist_ok=True)

            self.qc_disabled_for_job = False
            qc_available = self.qc_worker.available()
            if qc_available:
                self._set_job(stage="Chargement Faster-Whisper Auto-QC…", qc_available=True)
                try:
                    qc_init = self.qc_worker.request({"cmd": "init", "model_path": self.config.get("qc_model_path")})
                    self._set_job(
                        qc_ready=True,
                        qc_device=str(qc_init.get("device") or ""),
                        qc_compute_type=str(qc_init.get("compute_type") or ""),
                    )
                except Exception as exc:
                    self.qc_worker.kill()
                    self.qc_disabled_for_job = True
                    qc_available = False
                    self._set_job(qc_ready=False, qc_warning=str(exc))

            accepted: list[Path] = []
            reports: list[dict[str, Any]] = []
            for i, chunk in enumerate(chunks, start=1):
                if self._cancelled():
                    raise RuntimeError("Génération annulée")
                base_progress = int((i - 1) * 94 / max(1, len(chunks)))
                self._set_job(
                    stage=f"Passage {i}/{len(chunks)}",
                    progress=base_progress,
                    chunk=i,
                    chunks=len(chunks),
                )
                paths, piece_reports = self._render_piece(
                    text=chunk,
                    voice_id=voice_id,
                    voice=voice,
                    speed=speed,
                    steps=steps,
                    language=language,
                    work_dir=work_dir,
                    label=f"Passage {i}/{len(chunks)}",
                )
                accepted.extend(paths)
                reports.extend(piece_reports)
                self._set_job(progress=int(i * 94 / max(1, len(chunks))))

            self._set_job(stage="Assemblage WAV validé…", progress=97)
            duration = self._merge_wavs(accepted, output, gap_ms=gap_ms)

            accepted_reports = [r for r in reports if r.get("accepted")]
            whisper_scores = [float(r.get("score") or 0.0) for r in accepted_reports if str(r.get("qc_mode") or "").startswith("whisper")]
            qc_avg = (sum(whisper_scores) / len(whisper_scores)) if whisper_scores else None
            qc_min = min(whisper_scores) if whisper_scores else None
            retries = max(0, len(reports) - len(accepted_reports))
            rescue_count = sum(1 for r in accepted_reports if r.get("soft_accept"))

            entry = {
                "id": filename,
                "filename": filename,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "text": text,
                "preview": text[:180] + ("…" if len(text) > 180 else ""),
                "voice_id": voice_id,
                "voice_name": voice.get("name") or voice_id,
                "engine": engine,
                "model": QWEN_MODEL_REPO if engine == "qwen" else "OmniVoice HQ",
                "speed": speed,
                "steps": steps if engine == "omnivoice" else None,
                "language": language,
                "duration": round(duration, 3),
                "elapsed": round(time.perf_counter() - started_all, 3),
                "chunks": len(accepted),
                "source_chunks": len(chunks),
                "retries": retries,
                "rescued_chunks": int(self.get_job().get("rescued_chunks") or 0),
                "soft_accepts": rescue_count,
                "qc_enabled": bool(whisper_scores),
                "qc_average": round(qc_avg, 4) if qc_avg is not None else None,
                "qc_min": round(qc_min, 4) if qc_min is not None else None,
                "qc_device": self.get_job().get("qc_device"),
                "quality_report": reports,
                "url": f"/history/{filename}",
            }
            history = read_json(HISTORY_DB, [])
            if not isinstance(history, list):
                history = []
            history.insert(0, entry)
            history = history[:50]
            write_json(HISTORY_DB, history)
            self._set_job(
                running=False,
                ready=True,
                stage="Audio prêt · contrôle qualité terminé",
                progress=100,
                result=entry,
                error="",
                cancel_requested=False,
            )
        except Exception as exc:
            cancelled = self._cancelled()
            if cancelled or str(exc) == "Génération annulée":
                self._set_job(running=False, stage="Annulé", error="", progress=0, cancel_requested=False)
            else:
                self._set_job(running=False, stage="Erreur", error=str(exc), progress=0)
        finally:
            # Faster-Whisper is kept for the whole script, then released so it does
            # not permanently consume VRAM next to the selected TTS engine / Ollama.
            self.qc_worker.stop()
            if work_dir is not None:
                try:
                    shutil.rmtree(work_dir, ignore_errors=True)
                except Exception:
                    pass

    def cancel_generate(self) -> dict[str, Any]:
        with self.job_lock:
            if not self.job.get("running"):
                return {"ok": True}
            self.job.update({"running": False, "stage": "Annulé", "error": "", "progress": 0, "cancel_requested": True})
        self.worker.kill()
        self.qc_worker.kill()
        # Restart only the selected TTS engine. QC remains lazy and frees VRAM.
        threading.Thread(target=self._warmup, args=(self.active_engine,), daemon=True).start()
        return {"ok": True}

    def get_history(self) -> list[dict[str, Any]]:
        history = read_json(HISTORY_DB, [])
        if not isinstance(history, list):
            return []
        valid = []
        for item in history[:50]:
            if Path(HISTORY_DIR / str(item.get("filename") or "")).is_file():
                valid.append(item)
        return valid

    def save_audio(self, filename: str) -> dict[str, Any]:
        """Copy a generated WAV to the user's Downloads folder.

        No GUI bridge is used here. The operation is invoked through the local
        HTTP API, so it keeps working even if PyWebView changes its JS bridge.
        """
        source = HISTORY_DIR / Path(str(filename or "")).name
        if not source.is_file():
            return {"ok": False, "error": "Audio introuvable."}
        target_dir = Path.home() / "Downloads"
        if not target_dir.is_dir():
            target_dir = Path.home() / "Desktop"
        if not target_dir.is_dir():
            target_dir = HISTORY_DIR
        target = target_dir / source.name
        stem, suffix = source.stem, source.suffix
        index = 2
        while target.exists():
            target = target_dir / f"{stem}_{index}{suffix}"
            index += 1
        shutil.copy2(source, target)
        return {"ok": True, "path": str(target)}

    def delete_history(self, filename: str) -> dict[str, Any]:
        name = Path(str(filename or "")).name
        path = HISTORY_DIR / name
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        history = read_json(HISTORY_DB, [])
        if isinstance(history, list):
            history = [x for x in history if str(x.get("filename") or "") != name]
            write_json(HISTORY_DB, history)
        return {"ok": True, "history": self.get_history()}

    def open_history_folder(self) -> dict[str, Any]:
        try:
            if os.name == "nt":
                os.startfile(str(HISTORY_DIR))
            else:
                subprocess.Popen(["xdg-open", str(HISTORY_DIR)])
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def shutdown(self):
        self.qc_worker.stop()
        self.omni_worker.stop()
        self.qwen_worker.stop()


def main() -> int:
    server = LocalServer()
    api = AppAPI(server)
    server.bind_api(api)
    server.start()
    url = f"http://127.0.0.1:{server.port}/web/index.html"
    # PyWebView is now only the native window. No js_api bridge is required.
    window = webview.create_window(
        "Narrator Studio · Dubroom",
        url=url,
        width=1480,
        height=920,
        min_size=(1050, 680),
        background_color="#f7f7f5",
    )
    api.bind_window(window)
    try:
        webview.start(debug=False)
    finally:
        api.shutdown()
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
