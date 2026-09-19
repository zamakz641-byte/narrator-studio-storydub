NARRATOR STUDIO · STORYDUB v1.4 AUTO-QC
=======================================

Lancement :
  double-clique LANCER_NARRATOR_STUDIO.bat

Cette version garde l'architecture v1.3 (PyWebView = simple fenêtre, API HTTP locale)
et ajoute un pipeline TTS de contrôle qualité automatique :

  1. découpage court par phrase (~140 caractères max) ;
  2. génération OmniVoice HQ avec le prompt narrateur StoryDub existant ;
  3. contrôle acoustique du WAV ;
  4. transcription locale Faster-Whisper Turbo si le runtime StoryDub est présent ;
  5. comparaison texte attendu / texte entendu ;
  6. retry stochastique en 12 steps ;
  7. escalade automatique 20 puis 32 steps si nécessaire ;
  8. rescue split autour des virgules/clauses si un passage reste instable ;
  9. assemblage uniquement des chunks validés.

Le cas "After turning himself in, ..." est inclus dans SELF_TEST.py comme test de
régression : le passage est isolé et le rescue splitter peut le séparer à la virgule.

Runtimes réutilisés, sans téléchargement :
  - OmniVoice HQ local DubRoom
  - Faster-Whisper Turbo local DubRoom
  - les 2 omnivoice_prompt.pt de StoryDub

Si Faster-Whisper ne peut pas être chargé (VRAM/runtime), la génération ne casse pas :
elle retombe sur le contrôle acoustique + chunks courts. Le worker QC est arrêté à la
fin de chaque script pour libérer la VRAM.

Fichiers importants :
  app.py           orchestration, retries, QC, historique
  tts_worker.py    worker OmniVoice persistant, 1 chunk à la fois
  qc_worker.py     worker Faster-Whisper local
  quality_utils.py chunking, score texte, contrôle WAV
  config.json      réglages par défaut
  SELF_TEST.py     preflight + tests de régression
