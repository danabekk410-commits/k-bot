#!/usr/bin/env python3
"""Локальная расшифровка голосовых: faster-whisper small, CPU int8, русский."""
import os
import sys

from faster_whisper import WhisperModel

model = WhisperModel("small", device="cpu", compute_type="int8",
                     download_root=os.path.expanduser("~/.cache/faster-whisper"))
segments, _ = model.transcribe(sys.argv[1], language="ru", vad_filter=True, beam_size=5)
print(" ".join(s.text.strip() for s in segments).strip())
