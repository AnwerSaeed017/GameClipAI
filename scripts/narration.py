"""Audio finishing shared by render_short.py and render_long.py: narration track, sound effects,
ducking mix and two-pass loudness mastering.

EDL:
  "narration": {"engine": "elevenlabs" | "kokoro", "voice": "...", "speed": 1.0,
                "elevenlabs": {"model": "eleven_multilingual_v2", "stability": 0.35, "style": 0.45},
                "lines": [{"at": 1.0, "text": "..."},                    # absolute time in the final video
                          {"seg": 3, "at": 1.5, "text": "..."}]}         # 1.5 s after segment 3 starts
  "sfx": [{"seg": 3, "at": 0.0, "type": "whoosh" | "hit", "gain": -10}]   # synthesized - no licensing issues
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

SR = 24000
SFX_SR = 48000


def line_time(ln: dict, seg_starts: list[float], segments: list[dict] | None) -> float:
    """Where a narration line starts on the final timeline.
    - {"seg": i, "src_at": "35:24.5"}: anchored to the SOURCE moment it describes (speed ramps handled)
    - {"seg": i, "at": 1.5}: seconds after segment i starts
    - {"at": 12.0}: absolute"""
    if "src_at" in ln and segments is not None:
        from gc_common import parse_time
        seg = segments[ln["seg"]]
        src = parse_time(str(ln["src_at"])) if isinstance(ln["src_at"], str) else float(ln["src_at"])
        return seg_starts[ln["seg"]] + (src - float(seg["in"])) / float(seg.get("speed", 1.0)) + float(ln.get("at", 0))
    return float(ln.get("at", 0)) + (seg_starts[ln["seg"]] if "seg" in ln else 0.0)


def engine_for(nar: dict):
    import tts
    name = nar.get("engine", "kokoro")
    k = tts.engine(name, **nar.get("elevenlabs", {})) if name == "elevenlabs" else tts.engine()
    return k, nar.get("voice", tts.DEFAULT_VOICE[name])


def build(nar: dict, total: float, seg_starts: list[float], work: Path,
          segments: list[dict] | None = None) -> tuple[Path, list[str], list[tuple[str, float, float]]]:
    """Render every line (cached for ElevenLabs) and place it on one WAV.
    Returns (wav, warnings, words) - words = (word, start, end) on the final timeline, for subtitles."""
    from scipy.io import wavfile
    import tts
    k, voice = engine_for(nar)
    track = np.zeros(int((total + 2) * SR), np.float32)
    placed, warnings, all_words = [], [], []
    for ln in nar["lines"]:
        at = line_time(ln, seg_starts, segments)
        audio, sr = tts.speak(k, ln["text"], voice, nar.get("speed", 1.0))
        for w, ws, we in tts.words(k, ln["text"], voice, nar.get("speed", 1.0), len(audio) / sr):
            all_words.append((w, at + ws, at + we))
        if sr != SR:  # Kokoro and our ElevenLabs path both give 24 kHz; resample defensively otherwise
            idx = np.linspace(0, len(audio) - 1, int(len(audio) * SR / sr))
            audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
        s = int(at * SR)
        e = min(len(track), s + len(audio))
        track[s:e] += audio[: e - s]
        placed.append((at, at + len(audio) / SR, ln["text"]))
    placed.sort()
    for (a0, a1, t0), (b0, _b1, _t1) in zip(placed, placed[1:]):
        if a1 > b0:
            warnings.append(f"narration overlap: '{t0[:40]}...' ends {a1:.1f}s but the next line starts {b0:.1f}s")
    if placed and placed[-1][1] > total:
        warnings.append(f"narration runs past the end of the video ({placed[-1][1]:.1f}s > {total:.1f}s)")
    path = work / "narration.wav"
    wavfile.write(path, SR, (np.clip(track, -1, 1) * 32767).astype(np.int16))
    return path, warnings, sorted(all_words, key=lambda x: x[1])


# ------------------------------------------------------------------ sound effects (generated, royalty-free)

def _whoosh(rng) -> np.ndarray:
    """Air whoosh: band-passed noise whose centre sweeps up then down, soft attack, longer tail."""
    from scipy.signal import butter, sosfilt
    n = int(0.7 * SFX_SR)
    noise = rng.normal(0, 1, n).astype(np.float32)
    out = np.zeros(n, np.float32)
    chunks = 28
    for i in range(chunks):
        a, b = i * n // chunks, (i + 1) * n // chunks
        p = i / (chunks - 1)
        fc = 400 + 2600 * np.sin(np.pi * min(1.0, p * 1.4)) ** 2
        sos = butter(2, [fc * 0.6, fc * 1.6], btype="band", fs=SFX_SR, output="sos")
        out[a:b] = sosfilt(sos, noise[max(0, a - 2000):b])[-(b - a):]
    t = np.linspace(0, 1, n)
    env = np.minimum(1, t / 0.45) ** 2 * np.exp(-np.maximum(0, t - 0.45) * 7)
    out *= env
    return out / (np.abs(out).max() + 1e-9)


def _hit(rng) -> np.ndarray:
    """Impact: decaying low sine thump plus a short noise transient."""
    n = int(0.6 * SFX_SR)
    t = np.arange(n) / SFX_SR
    thump = np.sin(2 * np.pi * (55 + 40 * np.exp(-t * 18)) * t) * np.exp(-t * 7)
    click = rng.normal(0, 1, n) * np.exp(-t * 60) * 0.5
    out = (thump + click).astype(np.float32)
    return out / (np.abs(out).max() + 1e-9)


SFX = {"whoosh": _whoosh, "hit": _hit}


def build_sfx(events: list[dict], total: float, seg_starts: list[float], work: Path) -> Path | None:
    if not events:
        return None
    from scipy.io import wavfile
    rng = np.random.default_rng(7)
    track = np.zeros(int((total + 2) * SFX_SR), np.float32)
    for ev in events:
        at = float(ev["at"]) + (seg_starts[ev["seg"]] if "seg" in ev else 0.0)
        snd = SFX[ev.get("type", "whoosh")](rng) * (10 ** (float(ev.get("gain", -10)) / 20))
        if ev.get("type", "whoosh") == "whoosh":
            at -= 0.45  # whoosh peaks ON the cut, so it starts a little before it
        s = max(0, int(at * SFX_SR))
        e = min(len(track), s + len(snd))
        track[s:e] += snd[: e - s]
    path = work / "sfx.wav"
    wavfile.write(path, SFX_SR, (np.clip(track, -1, 1) * 32767).astype(np.int16))
    return path


# ------------------------------------------------------------------ mix + two-pass loudness

def master(ff: str, run, log, joined: Path, nar_wav: Path | None, sfx_wav: Path | None, work: Path) -> Path:
    """Mix game (+ narration with ducking) (+ sfx), then two-pass loudnorm to -14 LUFS / -1.5 dBTP.
    Returns the mastered WAV to mux with the video."""
    inputs, labels = ["-i", str(joined)], []
    graph = ""
    idx = 1
    if nar_wav:
        inputs += ["-i", str(nar_wav)]
        # game audio ducks ~10 dB under the narrator
        graph += (f"[{idx}:a]aresample=48000,volume=1.6,asplit=2[nar][key];"
                  "[0:a][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[game];")
        labels += ["[game]", "[nar]"]
        idx += 1
    else:
        labels += ["[0:a]"]
    if sfx_wav:
        inputs += ["-i", str(sfx_wav)]
        graph += f"[{idx}:a]aresample=48000[fx];"
        labels += ["[fx]"]
    graph += f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0[mix]"
    mix = work / "mix.wav"
    run([ff, "-y", "-hide_banner", *inputs, "-filter_complex", graph, "-map", "[mix]", "-ar", "48000", str(mix)],
        log)
    # Game audio is very peaky (gunshots, explosions): plain gain to -14 LUFS would clip, and loudnorm's
    # linear mode refuses. So: measure -> gain -> brick-wall limiter at -2 dBFS -> re-measure -> correct once.
    def loudness(path: Path) -> float:
        err = run([ff, "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
                  log)
        return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", err)[-1])

    target, mastered = -14.0, work / "mastered.wav"
    gain = target - loudness(mix)
    for _ in range(6):
        # limiter runs at 4x sample rate so it also catches inter-sample (true) peaks; -3 dBFS leaves room
        # for the AAC encode's overshoot -> final true peak stays below -1 dBTP
        run([ff, "-y", "-hide_banner", "-i", str(mix), "-af",
             f"volume={gain:.2f}dB,aresample=192000,alimiter=limit=0.708:attack=1:release=60:level=disabled,"
             "aresample=48000", str(mastered)], log)
        got = loudness(mastered)
        if abs(got - target) < 0.4:
            break
        gain += target - got  # the limiter shaved some loudness; push the gain a little more
    return mastered
