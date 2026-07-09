"""wombat.sinks — delivery-side adapters that the drain/brief pathways speak through (TK-164, Q-96).

``tts_adapter.py`` defines the swappable local-TTS seam (``TTSAdapter`` Protocol + the concrete
``Pyttsx3Adapter``, lazy-imported so the optional ``voice`` extra is never a hard dependency);
``speak.py`` defines ``SpeakSink``, the drain pathway's terminal voice stage built over it.
"""
