r"""Narration engines behind one interface: Kokoro (local, free) and ElevenLabs (cloud, expressive).

  python scripts/tts.py samples                                    # Kokoro: 7 voices -> exports/narration/samples/
  python scripts/tts.py voices                                     # ElevenLabs: list the voices on your account
  python scripts/tts.py samples --engine elevenlabs --voices "Adam,Rachel"   # costs credits (once - cached)
  python scripts/tts.py say "Text" --engine elevenlabs --voice Adam --out exports/narration/line.wav
  python scripts/tts.py file script.txt --voice am_michael --out exports/narration/vo.wav
      (one paragraph per blank-line-separated block; a short pause is added between blocks)

Kokoro: models/kokoro/*.onnx + voices-v1.0.bin (see docs/SETUP.md). Runs offline.
ElevenLabs: put ELEVENLABS_API_KEY=... in a .env file in the project root (never committed) or in the
environment. Every generated line is cached in .cache/tts/, so re-rendering a video never spends credits twice.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "models" / "kokoro" / "kokoro-v1.0.onnx"
MODEL_HF = ROOT / "models" / "kokoro" / "kokoro-v1.0-hf.onnx"        # full-quality export (Hugging Face)
MODEL_INT8 = ROOT / "models" / "kokoro" / "kokoro-v1.0.int8-hf.onnx"  # smaller quantized export (Hugging Face)
VOICES = ROOT / "models" / "kokoro" / "voices-v1.0.bin"
OUT = ROOT / "exports" / "narration"
CACHE = ROOT / ".cache" / "tts"
SR = 24000

SAMPLE_TEXT = ("Three hundred attempts. Three hundred deaths. "
               "And then, on the last sliver of health, everything finally clicked.")
SAMPLE_VOICES = {
    "am_michael": "US male, calm narrator",
    "am_fenrir": "US male, deeper",
    "am_puck": "US male, energetic",
    "bm_george": "British male, documentary",
    "bm_fable": "British male, storyteller",
    "af_heart": "US female, warm (top-rated voice)",
    "af_bella": "US female, expressive",
}
CHANNEL_VOICE = "HfRP3cIhYLmeNHeTvkWK"  # the channel's chosen ElevenLabs narrator (chosen 2026-11-19)
DEFAULT_VOICE = {"kokoro": "am_michael", "elevenlabs": CHANNEL_VOICE}
# ElevenLabs premade voices usable by name even when the API key lacks the "Voices: Read" permission.
# Adam was verified live on 2026-11-19; for others, run `tts.py voices` (needs that permission) or pass the ID.
PREMADE = {"adam": "pNInz6obpgDQGcFmaJgB", "rachel": "21m00Tcm4TlvDq8ikWAM", "brian": "nPczCjzI2devNBz1zQrb"}


# ------------------------------------------------------------------ ElevenLabs

def _api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    env = ROOT / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            if line.strip().startswith("ELEVENLABS_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise SystemExit("ElevenLabs: no API key. Add ELEVENLABS_API_KEY=... to the .env file in the project root "
                         "(ElevenLabs website -> profile -> API keys). Never commit it.")
    return key


class ElevenLabs:
    """ElevenLabs text-to-speech over its REST API (standard library only).

    model: eleven_multilingual_v2 (stable, natural) or eleven_v3 (most expressive; supports audio tags such
    as [excited] or [whispers] in the text). Settings: stability lower = more emotional range;
    style higher = more exaggerated delivery.
    """
    API = "https://api.elevenlabs.io/v1"

    def __init__(self, model: str = "eleven_multilingual_v2", stability: float = 0.35,
                 similarity: float = 0.8, style: float = 0.45, speaker_boost: bool = True):
        self.key = _api_key()
        self.model, self.settings = model, dict(stability=stability, similarity_boost=similarity, style=style,
                                                use_speaker_boost=speaker_boost)
        self._voices: list[dict] | None = None

    def _request(self, path: str, body: dict | None = None) -> bytes:
        req = urllib.request.Request(self.API + path, data=json.dumps(body).encode() if body else None,
                                     headers={"xi-api-key": self.key, "Content-Type": "application/json",
                                              "User-Agent": "gameclip-tts"},
                                     method="POST" if body else "GET")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code == 401 and "missing_permissions" in detail:
                perm = re.search(r"permission (\w+)", detail)
                raise SystemExit(
                    f"ElevenLabs: your API key is valid but lacks the '{perm.group(1) if perm else '?'}' permission. "
                    "Edit the key at elevenlabs.io -> API Keys and enable it (Text to Speech + Voices: Read), "
                    f"or pass a voice ID / one of: {', '.join(PREMADE)}.") from None
            raise SystemExit(f"ElevenLabs API error {e.code}: {detail}") from None

    def voices(self) -> list[dict]:
        if self._voices is None:
            data = json.loads(self._request("/voices"))
            self._voices = [{"name": v["name"], "voice_id": v["voice_id"], "category": v.get("category", ""),
                             "labels": v.get("labels") or {}} for v in data.get("voices", [])]
        return self._voices

    def voice_id(self, voice: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9]{20}", voice):
            return voice
        if voice.lower() in PREMADE:  # no lookup call needed
            return PREMADE[voice.lower()]
        for v in self.voices():
            if v["name"].lower() == voice.lower() or v["name"].lower().startswith(voice.lower() + " "):
                return v["voice_id"]
        raise SystemExit(f"ElevenLabs voice '{voice}' not found. Run: python scripts/tts.py voices")

    def _key(self, text: str, vid: str, settings: dict) -> str:
        return hashlib.sha256(json.dumps([text, vid, self.model, settings], sort_keys=True).encode()).hexdigest()[:24]

    def speak(self, text: str, voice: str, speed: float = 1.0) -> tuple[np.ndarray, int]:
        """Returns audio; also caches ElevenLabs' character alignment so word timings are available
        (see words()). Uses the with-timestamps endpoint - same price as plain TTS."""
        vid = self.voice_id(voice)
        settings = {**self.settings, "speed": max(0.7, min(1.2, speed))}
        key = self._key(text, vid, settings)
        cached, align = CACHE / f"el_{key}.wav", CACHE / f"el_{key}.json"
        if cached.exists():  # never pay twice for the same line
            sr, a = wavfile.read(cached)
            return a.astype(np.float32) / 32767, sr
        import base64
        data = json.loads(self._request(f"/text-to-speech/{vid}/with-timestamps?output_format=mp3_44100_128",
                                        {"text": text, "model_id": self.model, "voice_settings": settings}))
        mp3 = base64.b64decode(data["audio_base64"])
        from gc_common import ffbin
        pcm = subprocess.run([ffbin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                              "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"],
                             input=mp3, capture_output=True, check=True).stdout
        audio = np.frombuffer(pcm, np.float32).copy()
        save(cached, audio, SR)
        al = data.get("alignment") or data.get("normalized_alignment")
        if al:
            align.write_text(json.dumps(al), encoding="utf-8")
        return audio, SR

    def alignment(self, text: str, voice: str, speed: float = 1.0) -> dict | None:
        settings = {**self.settings, "speed": max(0.7, min(1.2, speed))}
        p = CACHE / f"el_{self._key(text, self.voice_id(voice), settings)}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def words(k, text: str, voice: str, speed: float, duration: float) -> list[tuple[str, float, float]]:
    """Spoken words with (start, end) seconds inside the line. Exact for ElevenLabs (character alignment);
    estimated from character counts otherwise. Audio tags like [excited] are not shown as words."""
    al = k.alignment(text, voice, speed) if isinstance(k, ElevenLabs) else None
    out: list[tuple[str, float, float]] = []
    if al:
        chars, st, en = al["characters"], al["character_start_times_seconds"], al["character_end_times_seconds"]
        cur, s0, e0, in_tag = "", None, None, False
        for c, s, e in zip(chars, st, en):
            if c == "[":
                in_tag = True
            if in_tag:
                if c == "]":
                    in_tag = False
                continue
            if c.isspace():
                if cur:
                    out.append((cur, s0, e0))
                cur, s0 = "", None
                continue
            cur += c
            s0 = s if s0 is None else s0
            e0 = e
        if cur:
            out.append((cur, s0, e0))
        return out
    plain = re.sub(r"\[[^\]]*\]", "", text).split()
    total = sum(len(w) + 1 for w in plain) or 1
    t = 0.0
    for w in plain:
        d = duration * (len(w) + 1) / total
        out.append((w, t, t + d))
        t += d
    return out


# ------------------------------------------------------------------ engine selection

def engine(name: str = "kokoro", **opts):
    """name: 'kokoro' (local) or 'elevenlabs' (opts: model, stability, similarity, style, speaker_boost)."""
    if name == "elevenlabs":
        return ElevenLabs(**opts)
    from kokoro_onnx import Kokoro
    # full model if complete (~310 MB), else the quantized one (~90 MB)
    for m, min_size in ((MODEL, 300_000_000), (MODEL_HF, 300_000_000), (MODEL_INT8, 80_000_000)):
        if m.exists() and m.stat().st_size >= min_size:
            print(f"model: {m.name}")
            return Kokoro(str(m), str(VOICES))
    raise SystemExit("No complete Kokoro model in models/kokoro yet (see docs/SETUP.md)")


def speak(k, text: str, voice: str, speed: float = 1.0) -> tuple[np.ndarray, int]:
    if isinstance(k, ElevenLabs):
        return k.speak(text, voice, speed)
    lang = "en-gb" if voice.startswith("b") else "en-us"
    samples, sr = k.create(text, voice=voice, speed=speed, lang=lang)
    return np.asarray(samples, np.float32), sr


def save(path: Path, samples: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(path, sr, (np.clip(samples, -1, 1) * 32767).astype(np.int16))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sm = sub.add_parser("samples")
    sm.add_argument("--voices", default="", help="ElevenLabs: comma-separated voice names")
    sub.add_parser("voices", help="list ElevenLabs voices on your account")
    s = sub.add_parser("say"); s.add_argument("text")
    f = sub.add_parser("file"); f.add_argument("path")
    for p in (s, f):
        p.add_argument("--voice")
        p.add_argument("--speed", type=float, default=1.0)
        p.add_argument("--out", required=True)
    for p in (sm, s, f):
        p.add_argument("--engine", choices=["kokoro", "elevenlabs"], default="kokoro")
        p.add_argument("--model", default="eleven_multilingual_v2", help="ElevenLabs model id")
    a = ap.parse_args()

    if a.cmd == "voices":
        for v in ElevenLabs().voices():
            lab = ", ".join(f"{k}: {val}" for k, val in v["labels"].items())
            print(f"{v['name']:<28} {v['voice_id']}  {v['category']:<12} {lab}")
        return

    k = engine(a.engine, model=a.model) if a.engine == "elevenlabs" else engine()
    if a.cmd == "samples":
        if a.engine == "elevenlabs":
            names = [n.strip() for n in a.voices.split(",") if n.strip()] or [DEFAULT_VOICE["elevenlabs"]]
            items = {n: "ElevenLabs" for n in names}
        else:
            items = SAMPLE_VOICES
        for v, desc in items.items():
            t0 = time.perf_counter()
            audio, sr = speak(k, SAMPLE_TEXT, v, 1.0)
            tag = f"el_{v.replace(' ', '_')}" if a.engine == "elevenlabs" else v
            save(OUT / "samples" / f"{tag}.wav", audio, sr)
            print(f"{v:<16} {desc:<36} {len(audio) / sr:4.1f}s audio in {time.perf_counter() - t0:4.1f}s")
        print(f"Listen in {OUT / 'samples'}")
        return

    voice = a.voice or DEFAULT_VOICE[a.engine]
    text = a.text if a.cmd == "say" else Path(a.path).read_text(encoding="utf-8")
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    parts, sr = [], SR
    for b in blocks:
        audio, sr = speak(k, " ".join(b.split()), voice, a.speed)
        parts += [audio, np.zeros(int(0.45 * sr), np.float32)]
    save(Path(a.out), np.concatenate(parts) if parts else np.zeros(1, np.float32), sr)
    print(f"Saved {a.out}")


if __name__ == "__main__":
    main()
