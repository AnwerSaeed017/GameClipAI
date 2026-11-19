r"""A/B test ElevenLabs settings on the channel voice. Output: exports/narration/voice_ab/*.wav

  python scripts/voice_ab.py
"""
from __future__ import annotations

from pathlib import Path

import tts

OUT = tts.ROOT / "exports" / "narration" / "voice_ab"
TEXT = ("Thirteen years waiting for GTA 6. And somehow, our first hours involved a seaplane disaster… "
        "and renovating a house.")
TAGGED = ("[excited] Thirteen years waiting for GTA 6. [laughs] And somehow, our first hours involved a "
          "seaplane disaster… and renovating a house.")

VARIANTS = [
    # name, model, text, speed, settings
    ("A_calm_clear_v2", "eleven_multilingual_v2", TEXT, 0.9, dict(stability=0.6, similarity=0.8, style=0.1)),
    ("B_energetic_v2", "eleven_multilingual_v2", TEXT, 0.95, dict(stability=0.4, similarity=0.8, style=0.45)),
    ("C_natural_v3", "eleven_v3", TEXT, 0.95, dict(stability=0.5, similarity=0.8, style=0.0)),
    ("D_excited_v3", "eleven_v3", TAGGED, 1.0, dict(stability=0.5, similarity=0.8, style=0.0)),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, model, text, speed, st in VARIANTS:
        try:
            k = tts.ElevenLabs(model=model, **st)
            audio, sr = tts.speak(k, text, tts.CHANNEL_VOICE, speed)
            tts.save(OUT / f"{name}.wav", audio, sr)
            print(f"{name:<20} {len(audio) / sr:4.1f}s  ({len(text.split()) / (len(audio) / sr):.1f} words/s)")
        except SystemExit as e:
            print(f"{name:<20} FAILED: {e}")
    print(f"Listen in {OUT}")


if __name__ == "__main__":
    main()
