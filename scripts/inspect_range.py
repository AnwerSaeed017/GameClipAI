r"""Dense look at specific time ranges (pass 2 of the review): one frame every N seconds, HDR tone-mapped,
laid out as labelled sheets. Uses fast keyframe seeks in parallel, so sparse sampling of a 4K file is cheap.

  python scripts/inspect_range.py input\gta6_x.webm 18:00-21:50 34:10-38:20 --every 3
  -> analysis/<recording>/ranges/<start>-<end>.jpg
"""
from __future__ import annotations

import argparse
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from gc_common import ANALYSIS_DIR, ffbin, hwaccel_args, parse_time, probe, video_filter

TW, TH, COLS = 384, 216, 6


def grab(info, t: float, hw: str) -> np.ndarray | None:
    cmd = [ffbin("ffmpeg"), "-hide_banner", "-loglevel", "error", *hwaccel_args(hw), "-ss", f"{t:.2f}",
           "-i", str(info.path), "-frames:v", "1", "-vf", video_filter(info, TW, TH, None, hw),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    out = subprocess.run(cmd, capture_output=True).stdout
    return np.frombuffer(out, np.uint8).reshape(TH, TW, 3) if len(out) == TW * TH * 3 else None


def fmt(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def sheet(info, start: float, end: float, every: float, hw: str, out: Path) -> Path:
    times = list(np.arange(start, end + 1e-6, every))
    with ThreadPoolExecutor(max_workers=4) as pool:
        frames = list(pool.map(lambda t: grab(info, t, hw), times))
    rows = (len(times) + COLS - 1) // COLS
    img = Image.new("RGB", (COLS * TW, rows * TH), (12, 12, 12))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arialbd.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    for k, (t, f) in enumerate(zip(times, frames)):
        x, y = (k % COLS) * TW, (k // COLS) * TH
        if f is not None:
            img.paste(Image.fromarray(f), (x, y))
        d.rectangle([x + 3, y + 3, x + 78, y + 26], fill=(0, 0, 0))
        d.text((x + 6, y + 5), fmt(t), fill=(255, 220, 0), font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=82)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("ranges", nargs="+", help="start-end, e.g. 18:00-21:50")
    ap.add_argument("--every", type=float, default=3.0)
    a = ap.parse_args()
    info = probe(Path(a.video))
    hw = "d3d11va" if (info.is_hdr or "10" in info.pix_fmt) else "none"
    for r in a.ranges:
        s, e = (parse_time(x) for x in r.split("-"))
        out = ANALYSIS_DIR / Path(a.video).stem / "ranges" / f"{r.replace(':', '')}.jpg"
        print(sheet(info, s, e, a.every, hw, out))


if __name__ == "__main__":
    main()
