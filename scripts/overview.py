r"""Browse footage at a glance.

Many short clips - one mosaic, one row per clip:
  python scripts/overview.py input --prefix elden_ring --frames 10
  -> analysis/overview_elden_ring.jpg

One long session - the WHOLE recording as timestamped pages (for Claude to find moments the
detector missed, not just review its picks):
  python scripts/overview.py input\gta6_session1.webm --every 10
  -> analysis/<recording>/timeline/page_01.jpg ...  (6x5 = 30 frames per page = 5 min at 10 s)
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from gc_common import ANALYSIS_DIR, iter_frames, list_videos, probe

TW, TH, LABEL = 240, 135, 150


def timeline(video: Path, every: float, hwaccel: str = "auto") -> None:
    """Whole-session pages: 6 columns x 5 rows of 320x180 frames, each stamped with its time."""
    info = probe(video)
    if hwaccel == "auto":
        hwaccel = "d3d11va" if (info.is_hdr or "10" in info.pix_fmt) else "none"
    cols, rows, tw, th = 6, 5, 320, 180
    out = ANALYSIS_DIR / video.stem / "timeline"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("page_*.jpg"):  # our own previous output only
        old.unlink()
    try:
        font = ImageFont.truetype("arialbd.ttf", 16)
    except OSError:
        font = ImageFont.load_default()
    page, idx, n_pages = None, 0, 0

    def flush():
        nonlocal page, n_pages
        if page is not None:
            n_pages += 1
            page.save(out / f"page_{n_pages:02d}.jpg", quality=82)
            page = None

    for f in iter_frames(info, tw, th, 1.0 / every, hwaccel=hwaccel):
        k = idx % (cols * rows)
        if k == 0:
            flush()
            page = Image.new("RGB", (cols * tw, rows * th), (10, 10, 10))
        x, y = (k % cols) * tw, (k // cols) * th
        page.paste(Image.fromarray(f), (x, y))
        d = ImageDraw.Draw(page)
        t = idx * every
        label = f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}" if t >= 3600 else \
            f"{int(t // 60):02d}:{int(t % 60):02d}"
        d.rectangle([x + 3, y + 3, x + 12 + 9 * len(label), y + 24], fill=(0, 0, 0))
        d.text((x + 6, y + 5), label, fill=(255, 220, 0), font=font)
        idx += 1
    flush()
    print(f"{n_pages} pages ({idx} frames, every {every:g}s) -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="folder of clips, or a single long recording with --every")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--frames", type=int, default=10)
    ap.add_argument("--every", type=float, help="single-recording timeline: one frame every N seconds")
    a = ap.parse_args()
    if a.every:
        timeline(Path(a.folder), a.every)
        return
    vids = [v for v in list_videos(Path(a.folder)) if v.stem.startswith(a.prefix)]
    if not vids:
        raise SystemExit("no matching clips")
    sheet = Image.new("RGB", (LABEL + a.frames * TW, len(vids) * TH), (15, 15, 15))
    d = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
    for r, v in enumerate(vids):
        info = probe(v)
        fps = a.frames / max(info.duration, 0.1)
        d.text((6, r * TH + 6), v.stem.replace("_", " ")[:24], fill=(230, 230, 230), font=font)
        d.text((6, r * TH + 26), f"{info.duration:.1f}s  {info.fps:.0f}fps", fill=(150, 150, 150), font=font)
        for i, f in enumerate(iter_frames(info, TW, TH, fps)):
            if i >= a.frames:
                break
            x, y = LABEL + i * TW, r * TH
            sheet.paste(Image.fromarray(f), (x, y))
            d.rectangle([x + 2, y + 2, x + 44, y + 17], fill=(0, 0, 0))
            d.text((x + 4, y + 3), f"{i / fps:.1f}s", fill=(255, 220, 0), font=font)
    out = ANALYSIS_DIR / f"overview_{a.prefix or 'all'}.jpg"
    sheet.save(out, quality=85)
    print(out)


if __name__ == "__main__":
    main()
