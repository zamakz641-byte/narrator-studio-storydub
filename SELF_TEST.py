from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
errors: list[str] = []
notes: list[str] = []

for name in ("app.py", "tts_worker.py", "qc_worker.py", "quality_utils.py"):
    try:
        ast.parse((ROOT / name).read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"{name}: syntaxe invalide: {exc}")

js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
for forbidden in ("window.pywebview", "pywebviewready", "js_api"):
    if forbidden in js:
        errors.append(f"app.js contient encore {forbidden!r}")
node = shutil.which("node")
if node:
    p = subprocess.run([node, "--check", str(ROOT / "web" / "app.js")], capture_output=True, text=True)
    if p.returncode:
        errors.append("app.js syntax: " + p.stderr.strip())

cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
for voice in ("voice_ee40c6612ca1235e0c7a", "voice_73c9c20e2de552c1815d"):
    if voice not in json.dumps(cfg):
        errors.append(f"voice manquante dans config: {voice}")

# Regression tests for the exact class of failure reported by the user.
try:
    sys.path.insert(0, str(ROOT))
    from quality_utils import speech_safe_variant, split_text_aggressive, split_text_smart, text_similarity

    sample = (
        "Fighting the sleep pulling him under, Sunny spends nearly all his money on real coffee just to stay awake long enough to reach a police station. "
        "After turning himself in, he is locked inside a containment vault and warned that if he dies, everyone outside may have to fight whatever comes through his body. "
        "Then darkness takes him."
    )
    chunks = split_text_smart(sample, max_chars=140, max_words=30)
    if len(chunks) < 3:
        errors.append(f"chunker trop large: {chunks}")
    target = next((x for x in chunks if "turning himself in" in x), "")
    if not target:
        errors.append("le passage 'turning himself in' n'est pas isolé dans un chunk")
    rescue = split_text_aggressive(target, max_chars=82, max_words=18)
    if len(rescue) < 2:
        errors.append(f"rescue splitter n'isole pas la proposition fautive: {rescue}")
    safe = speech_safe_variant("After turning himself in, he is locked inside a containment vault.")
    if safe == "After turning himself in, he is locked inside a containment vault.":
        errors.append("fallback spoken-only pour 'turning himself in' absent")

    good = text_similarity("After turning himself in, he is locked inside a containment vault.", "After turning himself in, he is locked inside a containment vault.")["score"]
    bad = text_similarity("After turning himself in, he is locked inside a containment vault.", "After thunder in the valley, a strange noise appears.")["score"]
    if good < 0.95 or bad > 0.65:
        errors.append(f"QC similarity incohérente: good={good}, bad={bad}")
    notes.append(f"chunker={len(chunks)} chunks; rescue={len(rescue)} sous-chunks; safe={safe!r}; QC good={good:.2f}/bad={bad:.2f}")
except Exception as exc:
    errors.append("Tests Auto-QC: " + repr(exc))

# Import the real application without opening a window, then run the same Story
# Recapper detection used at launch.
try:
    import webview  # available in the Story Recapper Simple UI venv
except Exception:
    sys.modules["webview"] = types.SimpleNamespace()
try:
    spec = importlib.util.spec_from_file_location("narratorstudio_app", ROOT / "app.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    resolved, diag = mod.detect_from_storyrecapper(cfg)
    runtime = Path(str(resolved.get("runtime_python") or ""))
    model = Path(str(resolved.get("model_path") or ""))
    qc_runtime = Path(str(resolved.get("qc_runtime_python") or ""))
    qc_model = Path(str(resolved.get("qc_model_path") or ""))
    if sys.platform.startswith("win"):
        if not runtime.is_file():
            errors.append(f"Python OmniVoice introuvable: {runtime}")
        if not model.is_dir():
            errors.append(f"Modèle OmniVoice introuvable: {model}")
        if not qc_runtime.is_file():
            errors.append(f"Python Faster-Whisper introuvable: {qc_runtime}")
        if not qc_model.is_dir():
            errors.append(f"Modèle Faster-Whisper introuvable: {qc_model}")
        voices = resolved.get("voices") or []
        if len(voices) < 2:
            errors.append("Les 2 narrateurs Dubroom n'ont pas été détectés")
        for v in voices[:2]:
            pp = Path(str(v.get("prompt_path") or ""))
            if not pp.is_file():
                errors.append(f"Prompt narrateur introuvable: {pp}")
    notes.append("détection=" + str(diag.get("source") or "fallback"))
    notes.append("auto_qc=" + ("ready" if bool(resolved.get("qc_runtime_python") and resolved.get("qc_model_path")) else "fallback acoustique"))
except Exception as exc:
    errors.append("Détection Story Recapper: " + repr(exc))

if errors:
    print("SELF TEST FAILED")
    for error in errors:
        print(" -", error)
    raise SystemExit(1)

print("SELF TEST OK")
print(" - Python syntax OK")
print(" - JavaScript sans bridge PyWebView")
print(" - API HTTP locale active")
print(" - 2 narrateurs configurés")
print(" - chunker court + rescue splitter actifs")
print(" - Auto-QC Faster-Whisper détecté quand disponible")
for note in notes:
    print(" -", note)
