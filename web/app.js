(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    bootstrap: null,
    engine: "omnivoice",
    selectedVoice: { omnivoice: "", qwen: "" },
    history: [],
    job: {},
    lastResultId: "",
    lastError: "",
    qwen: null,
    cloning: false,
  };

  const els = {
    text: $("textInput"), counter: $("counter"), paste: $("pasteBtn"), clear: $("clearBtn"),
    folder: $("folderBtn"), runtimePill: $("runtimePill"), runtimeText: $("runtimeText"),
    progress: $("progressBar"), status: $("statusText"), pct: $("statusPct"),
    generate: $("generateBtn"), cancel: $("cancelBtn"), engine: $("engineSelect"), engineHint: $("engineHint"),
    voiceCards: $("voiceCards"), voiceChip: $("voiceEngineChip"), noQwenVoices: $("noQwenVoices"),
    speed: $("speedRange"), speedValue: $("speedValue"), speedSection: $("speedSection"), qwenSpeedHint: $("qwenSpeedHint"),
    steps: $("stepsSelect"), stepsSection: $("stepsSection"), language: $("languageSelect"),
    gpu: $("gpuText"), runtimeCheck: $("runtimeCheck"), voiceCheck: $("voiceCheck"), qcCheck: $("qcCheck"),
    playerCard: $("playerCard"), player: $("audioPlayer"), playerTitle: $("playerTitle"), playerMeta: $("playerMeta"), save: $("saveBtn"),
    history: $("historyList"), refreshHistory: $("refreshHistoryBtn"), toast: $("toast"),
    qwenRuntimeState: $("qwenRuntimeState"), qwenModelState: $("qwenModelState"), qwenModelPath: $("qwenModelPath"),
    downloadQwen: $("downloadQwenBtn"), downloadState: $("downloadState"), downloadPanel: $("downloadPanel"),
    downloadLabel: $("downloadLabel"), downloadPercent: $("downloadPercent"), downloadProgress: $("downloadProgressBar"),
    downloadBytes: $("downloadBytes"), downloadSpeed: $("downloadSpeed"), downloadEta: $("downloadEta"), qwenDownloadHint: $("qwenDownloadHint"), qwenRuntimePath: $("qwenRuntimePath"),
    qwenModelPathInput: $("qwenModelPathInput"), useQwenModel: $("useQwenModelBtn"), qwenQ8Notice: $("qwenQ8Notice"),
    rescanQwenRuntime: $("rescanQwenRuntimeBtn"), useQwenRuntime: $("useQwenRuntimeBtn"), cloneName: $("cloneName"),
    cloneLanguage: $("cloneLanguage"), cloneAudio: $("cloneAudio"), cloneFileName: $("cloneFileName"),
    cloneBtn: $("cloneBtn"), cloneStatus: $("cloneStatus"), transcriptBox: $("transcriptBox"), cloneTranscript: $("cloneTranscript"),
  };

  function escapeText(value) { return String(value ?? ""); }
  function fmtSeconds(v) {
    const n = Number(v || 0);
    if (!Number.isFinite(n) || n <= 0) return "—";
    const m = Math.floor(n / 60), s = Math.round(n % 60);
    return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
  }
  function fmtScore(v) {
    const n = Number(v);
    return Number.isFinite(n) ? `${Math.round(n * 100)}%` : "—";
  }
  function fmtBytes(v) {
    const n = Number(v || 0);
    if (!Number.isFinite(n) || n <= 0) return "0 Mo";
    if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} Go`;
    return `${(n / 1024 ** 2).toFixed(n >= 100 * 1024 ** 2 ? 0 : 1)} Mo`;
  }
  function fmtRate(v) {
    const n = Number(v || 0);
    return n > 0 ? `${fmtBytes(n)}/s` : "";
  }
  function fmtEta(v) {
    const n = Number(v);
    if (!Number.isFinite(n) || n < 0) return "";
    if (n < 60) return `${Math.ceil(n)} s restantes`;
    return `${Math.ceil(n / 60)} min restantes`;
  }

  async function api(path, options = {}) {
    const opts = { cache: "no-store", ...options };
    opts.headers = { ...(options.headers || {}) };
    if (opts.body && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { data = { ok: false, error: `HTTP ${res.status}` }; }
    if (!res.ok || data?.ok === false) throw new Error(data?.error || `HTTP ${res.status}`);
    return data;
  }

  function toast(message) {
    els.toast.textContent = escapeText(message);
    els.toast.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => els.toast.classList.remove("show"), 2600);
  }

  function setCounter() {
    const text = els.text.value || "";
    const words = text.trim() ? text.trim().split(/\s+/).length : 0;
    els.counter.textContent = `${text.length.toLocaleString("fr-FR")} caractères · ${words.toLocaleString("fr-FR")} mots`;
    updateGenerateButton();
  }

  function activeVoices() {
    if (!state.bootstrap) return [];
    return state.engine === "qwen" ? (state.bootstrap.qwen_voices || []) : (state.bootstrap.voices || []);
  }

  function ensureSelectedVoice() {
    const voices = activeVoices();
    const current = state.selectedVoice[state.engine];
    if (!voices.some(v => String(v.id) === String(current))) {
      state.selectedVoice[state.engine] = voices.length ? String(voices[0].id) : "";
    }
  }

  function renderVoices() {
    ensureSelectedVoice();
    els.voiceCards.replaceChildren();
    const voices = activeVoices();
    const selected = state.selectedVoice[state.engine];
    els.noQwenVoices.classList.toggle("hidden", !(state.engine === "qwen" && voices.length === 0));
    els.voiceChip.textContent = state.engine === "qwen" ? "Qwen 0.6B" : "OmniVoice";

    for (const voice of voices) {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "voice-card" + (String(voice.id) === String(selected) ? " active" : "");
      const avatar = document.createElement("span"); avatar.className = "avatar";
      const main = document.createElement("span"); main.className = "voice-main";
      const name = document.createElement("div"); name.className = "voice-name"; name.textContent = escapeText(voice.name || voice.id);
      const id = document.createElement("div"); id.className = "voice-id"; id.textContent = escapeText(voice.id);
      const eng = document.createElement("div"); eng.className = "voice-engine"; eng.textContent = state.engine === "qwen" ? "Qwen clone · ICL" : "OmniVoice prompt";
      main.append(name, id, eng);
      const check = document.createElement("span"); check.className = "check"; check.textContent = "✓";
      card.append(avatar, main, check);
      card.addEventListener("click", () => {
        state.selectedVoice[state.engine] = String(voice.id);
        renderVoices();
      });
      els.voiceCards.appendChild(card);
    }
    els.voiceCheck.textContent = voices.length ? `${voices.length} prête${voices.length > 1 ? "s" : ""}` : "Aucune";
    updateGenerateButton();
  }

  function renderEngineControls() {
    const qwen = state.engine === "qwen";
    els.engine.value = state.engine;
    els.engineHint.textContent = qwen
      ? "Qwen3-TTS 0.6B Base natif · profils .pt réutilisables · ICL HQ + x-vector fallback · Auto-QC Faster-Whisper."
      : "OmniVoice HQ local · Auto-QC Faster-Whisper + retry adaptatif.";
    els.speed.disabled = qwen;
    if (qwen) {
      els.speed.value = "1.00";
      els.speedValue.textContent = "1.00×";
    }
    els.qwenSpeedHint.classList.toggle("hidden", !qwen);
    els.stepsSection.classList.toggle("hidden", qwen);
    renderVoices();
    renderRuntime();
  }

  function renderRuntime() {
    const rt = state.bootstrap?.runtime || {};
    const job = state.job || {};
    const qwen = state.engine === "qwen";
    const engineReady = qwen ? !!rt.qwen_ready : (!!rt.python_exists && !!rt.model_exists);
    const runtimeExists = qwen ? !!rt.qwen_python_exists : !!rt.python_exists;
    const modelExists = qwen ? !!rt.qwen_model_exists : !!rt.model_exists;

    els.gpu.textContent = escapeText(job.gpu || "CUDA");
    els.runtimeCheck.textContent = engineReady ? (qwen ? "Qwen 0.6B prêt" : "OmniVoice prêt") : (!runtimeExists ? "Runtime manquant" : (!modelExists ? "Poids manquants" : "Chargement…"));
    els.qcCheck.textContent = rt.qc_ready ? "Faster-Whisper Turbo" : "Indisponible";

    els.runtimePill.classList.remove("ready", "error");
    if (job.error) els.runtimePill.classList.add("error");
    else if (job.ready && job.active_engine === state.engine) els.runtimePill.classList.add("ready");
    els.runtimeText.textContent = escapeText(job.error || job.stage || (engineReady ? "Prêt" : "Runtime incomplet"));
    updateGenerateButton();
  }

  function updateGenerateButton() {
    const jobRunning = !!state.job?.running;
    const hasText = !!els.text.value.trim();
    const hasVoice = !!state.selectedVoice[state.engine];
    const rt = state.bootstrap?.runtime || {};
    const engineReady = state.engine === "qwen" ? !!rt.qwen_ready : (!!rt.python_exists && !!rt.model_exists);
    els.generate.disabled = jobRunning || !hasText || !hasVoice || !engineReady;
    els.cancel.classList.toggle("hidden", !jobRunning);
  }

  function showPlayer(entry) {
    if (!entry?.filename) return;
    state.lastResultId = String(entry.filename);
    els.playerCard.classList.remove("hidden");
    els.player.src = `/api/audio?filename=${encodeURIComponent(entry.filename)}&t=${Date.now()}`;
    els.playerTitle.textContent = `${entry.engine === "qwen" ? "Qwen 0.6B" : "OmniVoice"} · ${escapeText(entry.voice_name || "Audio généré")}`;
    const qc = entry.qc_enabled ? `QC ${fmtScore(entry.qc_average)} · min ${fmtScore(entry.qc_min)}` : "QC acoustique";
    els.playerMeta.textContent = `${fmtSeconds(entry.duration)} · ${entry.source_chunks || entry.chunks || 1} passages · ${qc}`;
  }

  function renderJob(job) {
    state.job = job || {};
    const progress = Math.max(0, Math.min(100, Number(job?.progress || 0)));
    els.progress.style.width = `${progress}%`;
    els.status.textContent = escapeText(job?.error || job?.stage || "Prêt");
    els.pct.textContent = job?.running || progress ? `${Math.round(progress)}%` : "";
    if (job?.error && job.error !== state.lastError) {
      state.lastError = job.error;
      toast(job.error);
    }
    if (!job?.error) state.lastError = "";
    if (job?.result?.filename && String(job.result.filename) !== state.lastResultId) {
      showPlayer(job.result);
      refreshHistory();
    }
    renderRuntime();
  }

  function historyButton(label, handler, danger = false) {
    const b = document.createElement("button"); b.type = "button"; b.textContent = label;
    if (danger) b.classList.add("danger"); b.addEventListener("click", handler); return b;
  }

  function renderHistory() {
    els.history.replaceChildren();
    const rows = state.history || [];
    if (!rows.length) {
      const empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "Aucune génération pour l’instant."; els.history.appendChild(empty); return;
    }
    for (const item of rows) {
      const card = document.createElement("div"); card.className = "history-item";
      const top = document.createElement("div"); top.className = "history-top";
      const title = document.createElement("strong"); title.textContent = `${item.engine === "qwen" ? "Qwen 0.6B" : "OmniVoice"} · ${escapeText(item.voice_name || "Voix")}`;
      const date = document.createElement("span"); date.textContent = escapeText(item.created_at || ""); top.append(title, date);
      const preview = document.createElement("div"); preview.className = "history-preview"; preview.textContent = escapeText(item.preview || item.text || "");
      const meta = document.createElement("div"); meta.className = "history-meta";
      const parts = [fmtSeconds(item.duration), `${item.source_chunks || item.chunks || 1} passages`, item.qc_enabled ? `QC ${fmtScore(item.qc_average)}` : "QC acoustique", item.retries ? `${item.retries} retry` : "0 retry"];
      for (const part of parts) { const sp = document.createElement("span"); sp.textContent = part; meta.appendChild(sp); }
      const actions = document.createElement("div"); actions.className = "history-actions";
      actions.append(
        historyButton("Écouter", () => showPlayer(item)),
        historyButton("Sauvegarder", () => saveFilename(item.filename)),
        historyButton("Supprimer", async () => {
          try { await api("/api/delete", { method: "POST", body: { filename: item.filename } }); await refreshHistory(); }
          catch (e) { toast(e.message); }
        }, true)
      );
      card.append(top, preview, meta, actions); els.history.appendChild(card);
    }
  }

  async function refreshHistory() {
    try { state.history = await api("/api/history"); renderHistory(); }
    catch (e) { console.warn(e); }
  }

  async function saveFilename(filename) {
    if (!filename) return;
    try { const r = await api("/api/save", { method: "POST", body: { filename } }); toast(`Sauvegardé : ${r.path}`); }
    catch (e) { toast(e.message); }
  }

  async function switchEngine(engine) {
    state.engine = engine === "qwen" ? "qwen" : "omnivoice";
    renderEngineControls();
    try {
      const r = await api("/api/engine", { method: "POST", body: { engine: state.engine } });
      state.engine = r.engine || state.engine;
    } catch (e) { toast(e.message); }
    renderEngineControls();
  }

  async function generate() {
    const text = els.text.value.trim();
    const voiceId = state.selectedVoice[state.engine];
    if (!text || !voiceId) return;
    try {
      state.lastResultId = "";
      const r = await api("/api/generate", {
        method: "POST",
        body: {
          text,
          engine: state.engine,
          voice_id: voiceId,
          speed: state.engine === "qwen" ? 1.0 : Number(els.speed.value || 1),
          steps: Number(els.steps.value || 12),
          language: els.language.value || "fr",
        },
      });
      if (r.ok) { state.job.running = true; updateGenerateButton(); }
    } catch (e) { toast(e.message); }
  }

  async function pollJob() {
    try { renderJob(await api("/api/job")); } catch (_) {}
  }

  function renderQwenStatus(data) {
    state.qwen = data;
    if (!data) return;
    const ready = !!data.ready;
    const running = !!data.download?.running;
    const progress = Math.max(0, Math.min(100, Number(data.download?.progress || 0)));
    els.qwenRuntimeState.textContent = data.runtime_python_exists ? "Prêt" : "Introuvable";
    els.qwenRuntimeState.className = data.runtime_python_exists ? "runtime-ok" : "runtime-bad";
    els.qwenModelState.textContent = data.model_exists ? "Validés" : "Manquants";
    els.qwenModelState.className = data.model_exists ? "runtime-ok" : "runtime-bad";
    els.qwenModelPath.textContent = escapeText(data.model_path || "—");
    if (els.qwenRuntimePath && !els.qwenRuntimePath.matches(":focus")) {
      els.qwenRuntimePath.value = data.runtime_python || "";
    }
    if (els.qwenModelPathInput && !els.qwenModelPathInput.matches(":focus")) {
      els.qwenModelPathInput.value = data.model_exists ? (data.model_path || "") : "";
    }
    if (els.qwenQ8Notice) {
      const showQ8 = !!data.q8_detected && !data.model_exists;
      els.qwenQ8Notice.classList.toggle("hidden", !showQ8);
      els.qwenQ8Notice.classList.remove("success", "error");
      if (showQ8) {
        els.qwenQ8Notice.classList.add("error");
        els.qwenQ8Notice.textContent = `Pack Q8 déjà trouvé : ${data.q8_path}. Il reste utilisable par KoboldCpp, mais ce GGUF ne contient pas le snapshot natif nécessaire pour créer un .pt Qwen. v1.8 ne le retélécharge pas et ne le supprime pas.`;
      }
    }
    const canDownload = !!data.download_capable || !!data.runtime_python_exists;
    // Once the official snapshot is complete, the download button disappears.
    els.downloadQwen.classList.toggle("hidden", !!data.model_exists);
    els.qwenDownloadHint?.classList.toggle("hidden", !!data.model_exists);
    els.downloadQwen.disabled = running || !canDownload;
    els.downloadQwen.title = canDownload ? "" : "Aucun Python existant avec huggingface_hub détecté";
    els.downloadQwen.textContent = running ? "Téléchargement en cours…" : "Télécharger Qwen 0.6B Base automatiquement";

    els.downloadPanel?.classList.toggle("hidden", !running);
    if (running && els.downloadPanel) {
      els.downloadLabel.textContent = escapeText(data.download?.stage || "Téléchargement…");
      els.downloadPercent.textContent = `${progress.toFixed(progress < 10 ? 1 : 0)}%`;
      els.downloadProgress.style.width = `${progress}%`;
      const done = Number(data.download?.bytes_done || 0), total = Number(data.download?.total_bytes || 0);
      els.downloadBytes.textContent = total > 0 ? `${fmtBytes(done)} / ${fmtBytes(total)}` : fmtBytes(done);
      els.downloadSpeed.textContent = fmtRate(data.download?.speed_bps);
      els.downloadEta.textContent = fmtEta(data.download?.eta_seconds);
    }
    if (data.download?.error) {
      els.downloadState.classList.remove("hidden", "success");
      els.downloadState.classList.add("error");
      els.downloadState.textContent = escapeText(data.download.error);
    } else if (data.model_exists) {
      els.downloadState.classList.remove("hidden", "error");
      els.downloadState.classList.add("success");
      els.downloadState.textContent = "Qwen3-TTS 0.6B Base officiel validé. Le téléchargement est terminé.";
    } else if (data.download?.stage && !running) {
      els.downloadState.classList.remove("hidden", "error", "success");
      els.downloadState.textContent = escapeText(data.download.stage);
    } else {
      els.downloadState.classList.add("hidden");
    }
    els.cloneBtn.disabled = state.cloning || !ready;
    if (state.bootstrap) {
      state.bootstrap.qwen_voices = data.voices || [];
      state.bootstrap.runtime.qwen_ready = ready;
      state.bootstrap.runtime.qwen_model_exists = !!data.model_exists;
      state.bootstrap.runtime.qwen_python_exists = !!data.runtime_python_exists;
      state.bootstrap.runtime.qwen_model_path = data.model_path;
      if (state.engine === "qwen") renderEngineControls();
    }
  }

  async function pollQwenStatus() {
    try { renderQwenStatus(await api("/api/qwen/status")); } catch (_) {}
  }

  function fileToDataUrl(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result || ""));
      r.onerror = () => reject(r.error || new Error("Lecture du fichier impossible"));
      r.readAsDataURL(file);
    });
  }

  async function createQwenClone() {
    if (state.cloning) return;
    const file = els.cloneAudio.files?.[0];
    const name = els.cloneName.value.trim();
    if (!name) return toast("Donne un nom à la voix.");
    if (!file) return toast("Choisis un audio de référence.");
    if (file.size > 28_000_000) return toast("Référence trop lourde : 28 Mo maximum.");

    state.cloning = true;
    els.cloneBtn.disabled = true;
    els.cloneStatus.classList.remove("hidden", "error", "success");
    els.cloneStatus.textContent = "Préparation de la référence…";
    try {
      const audioBase64 = await fileToDataUrl(file);
      els.cloneStatus.textContent = "Faster-Whisper transcrit la référence, puis Qwen extrait une fois ICL + x-vector et les enregistre en .pt…";
      const result = await api("/api/qwen/clone", {
        method: "POST",
        body: { name, language: els.cloneLanguage.value || "fr", filename: file.name, audio_base64: audioBase64 },
      });
      els.cloneStatus.classList.add("success");
      els.cloneStatus.textContent = `Voix « ${result.voice?.name || name} » enregistrée en .pt. Les prochaines générations ne ré-encodent plus le WAV.`;
      els.cloneTranscript.value = result.transcript || "";
      els.transcriptBox.classList.remove("hidden");
      await bootstrap();
      state.engine = "qwen";
      state.selectedVoice.qwen = String(result.voice?.id || "");
      els.engine.value = "qwen";
      renderEngineControls();
      toast("Profils Qwen .pt créés.");
    } catch (e) {
      els.cloneStatus.classList.add("error");
      els.cloneStatus.textContent = e.message;
    } finally {
      state.cloning = false;
      els.cloneBtn.disabled = false;
    }
  }

  async function bootstrap() {
    const data = await api("/api/bootstrap");
    state.bootstrap = data;
    state.history = data.history || [];
    state.job = data.job || {};
    if (!state.selectedVoice.omnivoice) state.selectedVoice.omnivoice = String(data.defaults?.voice_id || data.voices?.[0]?.id || "");
    if (!state.selectedVoice.qwen) state.selectedVoice.qwen = String(data.qwen_voices?.[0]?.id || "");
    state.engine = data.active_engine === "qwen" ? "qwen" : "omnivoice";
    els.engine.value = state.engine;
    renderEngineControls();
    renderJob(state.job);
    renderHistory();
    renderQwenStatus({
      ok: true,
      ready: !!data.runtime?.qwen_ready,
      runtime_python_exists: !!data.runtime?.qwen_python_exists,
      runtime_python: data.runtime?.qwen_runtime_python || "",
      download_capable: !!data.runtime?.qwen_download_capable,
      model_exists: !!data.runtime?.qwen_model_exists,
      model_path: data.runtime?.qwen_model_path,
      q8_detected: !!data.runtime?.qwen_q8_detected,
      q8_path: data.runtime?.qwen_q8_path || "",
      voices: data.qwen_voices || [],
      download: data.qwen_download || {},
      active: state.engine === "qwen",
    });
  }

  // Tabs
  document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === tab));
    document.querySelectorAll(".tab-page").forEach(x => x.classList.remove("active"));
    const page = $(`${tab.dataset.tab}Tab`);
    if (page) page.classList.add("active");
    if (tab.dataset.tab === "history") refreshHistory();
    if (tab.dataset.tab === "qwenClone") pollQwenStatus();
  }));

  els.text.addEventListener("input", setCounter);
  els.clear.addEventListener("click", () => { els.text.value = ""; setCounter(); els.text.focus(); });
  els.paste.addEventListener("click", async () => {
    try { els.text.value = await navigator.clipboard.readText(); setCounter(); } catch (_) { toast("Le presse-papiers n'est pas accessible ici."); }
  });
  els.speed.addEventListener("input", () => { els.speedValue.textContent = `${Number(els.speed.value).toFixed(2)}×`; });
  els.engine.addEventListener("change", () => switchEngine(els.engine.value));
  els.generate.addEventListener("click", generate);
  els.cancel.addEventListener("click", async () => { try { await api("/api/cancel", { method: "POST", body: {} }); } catch (e) { toast(e.message); } });
  els.folder.addEventListener("click", async () => { try { await api("/api/open-folder", { method: "POST", body: {} }); } catch (e) { toast(e.message); } });
  els.save.addEventListener("click", () => saveFilename(state.lastResultId));
  els.refreshHistory.addEventListener("click", refreshHistory);
  els.cloneAudio.addEventListener("change", () => { els.cloneFileName.textContent = els.cloneAudio.files?.[0]?.name || "Choisir un audio"; });
  els.rescanQwenRuntime?.addEventListener("click", async () => {
    try {
      els.rescanQwenRuntime.disabled = true;
      els.rescanQwenRuntime.textContent = "Recherche…";
      renderQwenStatus(await api("/api/qwen/rescan-runtime", { method: "POST", body: {} }));
      if (state.qwen?.ready) toast("Runtime et modèle Qwen natif détectés.");
      else if (state.qwen?.model_exists) toast("Modèle Qwen natif détecté. Vérifie le runtime.");
      else if (state.qwen?.q8_detected) toast("Q8 détecté, mais le snapshot natif .pt n'est pas présent.");
      else toast(state.qwen?.runtime_python_exists ? "Runtime Qwen détecté, modèle natif absent." : "qwen_tts n’a été trouvé dans aucun Python détecté.");
    } catch (e) { toast(e.message); }
    finally { els.rescanQwenRuntime.disabled = false; els.rescanQwenRuntime.textContent = "Re-détecter runtime + modèle"; }
  });
  els.useQwenRuntime?.addEventListener("click", async () => {
    const pythonPath = els.qwenRuntimePath?.value.trim() || "";
    if (!pythonPath) return toast("Colle le chemin du python.exe qui contient qwen_tts.");
    try {
      els.useQwenRuntime.disabled = true;
      await api("/api/qwen/runtime", { method: "POST", body: { python_path: pythonPath } });
      await pollQwenStatus();
      await bootstrap();
      toast("Runtime Qwen validé.");
    } catch (e) { toast(e.message); }
    finally { els.useQwenRuntime.disabled = false; }
  });

  els.useQwenModel?.addEventListener("click", async () => {
    const modelPath = els.qwenModelPathInput?.value.trim() || "";
    if (!modelPath) return toast("Colle le dossier du modèle Qwen Base déjà présent.");
    try {
      els.useQwenModel.disabled = true;
      renderQwenStatus(await api("/api/qwen/model-path", { method: "POST", body: { model_path: modelPath } }));
      await bootstrap();
      toast("Modèle Qwen natif validé.");
    } catch (e) { toast(e.message); }
    finally { els.useQwenModel.disabled = false; }
  });
  els.cloneBtn.addEventListener("click", createQwenClone);
  els.downloadQwen.addEventListener("click", async () => {
    try {
      els.downloadQwen.disabled = true;
      els.downloadState.classList.remove("hidden", "error", "success");
      els.downloadState.textContent = "Connexion au dépôt officiel Qwen…";
      await api("/api/qwen/download-model", { method: "POST", body: {} });
      await pollQwenStatus();
    } catch (e) {
      els.downloadState.classList.add("error");
      els.downloadState.textContent = e.message;
    }
  });

  setCounter();
  bootstrap().catch(e => {
    els.runtimePill.classList.add("error");
    els.runtimeText.textContent = e.message;
    toast(e.message);
  });
  setInterval(pollJob, 650);
  setInterval(pollQwenStatus, 1800);
})();
