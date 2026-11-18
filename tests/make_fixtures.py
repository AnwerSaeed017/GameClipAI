"""Generate synthetic test videos with KNOWN events (ground truth is by construction).

These only test that the software behaves correctly. They say nothing about real gameplay.
Output: tests/fixtures/*.mp4
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.io import wavfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from gc_common import ffbin  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
W, H, FPS, SR = 640, 360, 30, 48000
rng = np.random.default_rng(7)


def texture(seed: int, w=8000, h=600) -> np.ndarray:
    r = np.random.default_rng(seed)
    base = r.integers(0, 255, (h // 8, w // 8, 3), dtype=np.uint8)
    big = cv2.resize(base, (w, h), interpolation=cv2.INTER_CUBIC)
    return cv2.GaussianBlur(big, (0, 0), 3)


TEX_A, TEX_B = texture(1), texture(2)


def pan(tex, x, y=100):  # texture is wide enough that no fixture pan wraps around
    x = int(x) % (tex.shape[1] - W)
    return tex[y:y + H, x:x + W].copy()


def write_video(path: Path, duration: float, frame_fn, audio: np.ndarray | None, hdr=False) -> Path:
    FIX.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=FIX) as td:
        cmd = [ffbin("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-"]
        if audio is not None:
            wav = Path(td) / "a.wav"
            wavfile.write(wav, SR, (np.clip(audio, -1, 1) * 32767).astype(np.int16))
            cmd += ["-i", str(wav), "-c:a", "aac", "-b:a", "128k"]
        if hdr:
            cmd += ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
                    "-x265-params", "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
                    "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        cmd += ["-shortest", str(path)]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        for i in range(int(duration * FPS)):
            p.stdin.write(np.ascontiguousarray(frame_fn(i / FPS), dtype=np.uint8).tobytes())
        p.stdin.close()
        if p.wait() != 0:
            raise RuntimeError(f"ffmpeg failed for {path}")
    return path


def quiet(duration, level=0.004):
    return rng.normal(0, level, int(duration * SR)).astype(np.float32)


def burst(a, t0, t1, level=0.5):
    """Gunshot-like noise bursts, 8 per second."""
    for t in np.arange(t0, t1, 0.125):
        s = int(t * SR); n = int(0.05 * SR)
        a[s:s + n] += (rng.normal(0, level, n) * np.exp(-np.linspace(0, 6, n))).astype(np.float32)


def blob(frame, t):
    for k in range(3):
        cx = int(rng.integers(80, W - 80)); cy = int(rng.integers(60, H - 60))
        cv2.circle(frame, (cx, cy), 45, (255, 255, 255) if k % 2 == 0 else (255, 60, 0), -1)
    return frame


# ------------------------------------------------------------------ fixture 1: known events
# 0-20 slow pan, quiet | 20-30 static (menu) | 40-42 ACTION + loud bursts | 50-60 FAST pan only
# 60 hard cut | 60-100 loud steady music | 90-92 ACTION + bursts on top of music | 100-110 black | 110-120 pan
EVENTS_TRUTH = {"positives": [(40, 42), (90, 92)], "no_candidate_zones": [(21, 30), (51, 58), (72, 86), (101, 109)]}


def events_frame(t):
    if 20 <= t < 30:
        return pan(TEX_A, 20 * 40)
    if 100 <= t < 110:
        return np.full((H, W, 3), 3, np.uint8)
    if t < 50:
        f = pan(TEX_A, t * 40)
    elif t < 60:
        f = pan(TEX_A, 50 * 40 + (t - 50) * 500)
    else:
        f = pan(TEX_B, (t - 60) * 40)
    if 40 <= t < 42 or 90 <= t < 92:
        f = blob(f, t)
    return f


def events_audio():
    a = quiet(120)
    tt = np.arange(len(a)) / SR
    music = (0.12 * (np.sin(2 * np.pi * 220 * tt) + np.sin(2 * np.pi * 277 * tt) + np.sin(2 * np.pi * 330 * tt)))
    m = (tt >= 60) & (tt < 100)
    a[m] += music[m].astype(np.float32)
    burst(a, 40, 42)
    burst(a, 90, 92, level=0.8)
    a[(tt >= 100) & (tt < 110)] = 0
    return a


# ------------------------------------------------------------------ fixture 2: boring (expect 0 candidates)
def boring_frame(t):
    return pan(TEX_A, t * 40)


# ------------------------------------------------------------------ fixture 3: HUD markers
# boss bar on 10-26 (death banner 24-27 -> boss_death) and 40-60 (bar off at 60, victory banner 62-65)
MARKER_TRUTH = {"boss_death": 24.0, "boss_victory": 62.0}


def _font(size):
    try:
        return ImageFont.truetype("arialbd.ttf", size)
    except OSError:
        return ImageFont.load_default()


def overlay(frame, t):
    img = Image.fromarray(frame)
    d = ImageDraw.Draw(img)
    if 10 <= t < 26 or 40 <= t < 60:
        d.text((int(W * 0.2), int(H * 0.80)), "WARDEN OF ASH", fill=(240, 240, 240), font=_font(16))
        d.rectangle([int(W * 0.2), int(H * 0.86), int(W * 0.8), int(H * 0.89)], fill=(20, 20, 20),
                    outline=(200, 170, 90), width=2)
        d.rectangle([int(W * 0.2) + 3, int(H * 0.86) + 3, int(W * 0.65), int(H * 0.89) - 3], fill=(150, 10, 10))
    if 24 <= t < 27:
        arr = (np.asarray(img) * 0.3).astype(np.uint8)
        img = Image.fromarray(arr); d = ImageDraw.Draw(img)
        d.text((W // 2, H // 2), "YOU PERISHED", fill=(170, 20, 20), font=_font(44), anchor="mm")
    if 62 <= t < 65:
        d.text((W // 2, H // 2), "FOE VANQUISHED", fill=(220, 180, 60), font=_font(40), anchor="mm")
    return np.asarray(img)


def markers_frame(t):
    return overlay(pan(TEX_A, t * 40), t)


def main():
    write_video(FIX / "synthetic_events.mp4", 120, events_frame, events_audio())
    write_video(FIX / "synthetic_boring.mp4", 60, boring_frame, quiet(60))
    write_video(FIX / "synthetic_markers.mp4", 75, markers_frame, quiet(75))
    write_video(FIX / "synthetic_hdr.mp4", 15, boring_frame, quiet(15), hdr=True)
    write_video(FIX / "synthetic_noaudio.mp4", 20, boring_frame, None)
    print("fixtures written to", FIX)


if __name__ == "__main__":
    main()
