"""How much energy can we get out of Kokoro? Renders the same hook several ways for side-by-side listening.

Output: exports/narration/energy_test/*.wav
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
from scipy.io import wavfile

import tts
from gc_common import ffbin

OUT = tts.ROOT / "exports" / "narration" / "energy_test"

CALM = "Seven bosses. Seven kills. Here is every one of them."
PUNCHY = "Seven bosses. Seven kills! And number four? Absolutely brutal."

# broadcast-style polish: tighten dynamics, add presence and a little weight, then loud and clean
FX = ("highpass=f=80,equalizer=f=150:t=q:w=1:g=3,equalizer=f=3500:t=q:w=1.2:g=5,"
      "acompressor=threshold=0.08:ratio=4:attack=5:release=80:makeup=2,"
      "aexciter=amount=1.5:drive=6:freq=3000,loudnorm=I=-14:TP=-1.5")


def blend(k, a: str, b: str, wa: float):
    return wa * k.get_voice_style(a) + (1 - wa) * k.get_voice_style(b)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    k = tts.engine()
    variants = [
        ("1_before_calm_michael", CALM, "am_michael", 1.0, False),
        ("2_punchy_script_michael", PUNCHY, "am_michael", 1.12, False),
        ("3_punchy_puck_fast", PUNCHY, "am_puck", 1.15, False),
        ("4_punchy_fenrir_fast_fx", PUNCHY, "am_fenrir", 1.12, True),
        ("5_punchy_puck_fenrir_blend_fx", PUNCHY, ("am_puck", "am_fenrir", 0.6), 1.15, True),
    ]
    for name, text, voice, speed, fx in variants:
        style = blend(k, *voice) if isinstance(voice, tuple) else voice
        audio, sr = k.create(text, voice=style, speed=speed, lang="en-us")
        raw = OUT / f"{name}_raw.wav"
        wavfile.write(raw, sr, (np.clip(audio, -1, 1) * 32767).astype(np.int16))
        final = OUT / f"{name}.wav"
        af = FX if fx else "loudnorm=I=-14:TP=-1.5"
        subprocess.run([ffbin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw),
                        "-af", af, "-ar", "48000", str(final)], check=True)
        raw.unlink()
        print(f"{final.name:<40} {len(audio) / sr:4.1f}s")
    print(f"Listen in {OUT}")


if __name__ == "__main__":
    main()
