r"""Build game-profile markers from REAL footage (no guessed HUD positions).

1) Grab a frame with a grid to find where the banner / HUD element is:
     python scripts/make_template.py frame input\elden1.mp4 --t 12:34
   -> analysis/elden1/frames/t754.0_grid.jpg  (grid lines every 10%, labelled 0.0-1.0)

2) Cut a template from that frame and register it in a profile:
     python scripts/make_template.py crop input\elden1.mp4 --t 12:34 --roi 0.30 0.42 0.40 0.12 ^
         --profile elden_ring --id you_died --event death
   roi = x y w h as fractions of the frame. The search area saved in the profile is the
   roi plus --pad on every side, so small position changes still match.
   Add --also-t <time> (one or more) where the same banner appears again: only pixels that
   stay identical are compared, so the scene behind the banner doesn't matter.

Events: any name. A profile's "encounters" rules can combine them (see profiles/_example).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from gc_common import ANALYSIS_DIR, PROFILES_DIR, iter_frames, parse_time, probe


def grab(info, t: float, width: int) -> np.ndarray:
    h = int(round(info.height * width / info.width / 2) * 2)
    for f in iter_frames(info, width, h, None, start=t, duration=0.5):
        return f.copy()
    raise SystemExit(f"Could not decode a frame at {t}s")


def cmd_frame(args) -> None:
    info = probe(Path(args.video))
    t = parse_time(args.t)
    f = grab(info, t, args.width)
    out = ANALYSIS_DIR / Path(args.video).stem / "frames"
    out.mkdir(parents=True, exist_ok=True)
    Image.fromarray(f).save(out / f"t{t:.1f}.png")
    img = Image.fromarray(f)
    d = ImageDraw.Draw(img)
    H, W = f.shape[:2]
    for i in range(1, 10):
        x, y = W * i // 10, H * i // 10
        d.line([(x, 0), (x, H)], fill=(255, 255, 0), width=1)
        d.line([(0, y), (W, y)], fill=(255, 255, 0), width=1)
        d.text((x + 2, 2), f"{i/10:.1f}", fill=(255, 255, 0))
        d.text((2, y + 2), f"{i/10:.1f}", fill=(255, 255, 0))
    img.save(out / f"t{t:.1f}_grid.jpg", quality=90)
    print(f"Saved {out / f't{t:.1f}_grid.jpg'}")


def cmd_crop(args) -> None:
    info = probe(Path(args.video))
    pdir = PROFILES_DIR / args.profile
    (pdir / "templates").mkdir(parents=True, exist_ok=True)
    ppath = pdir / "profile.json"
    profile = json.loads(ppath.read_text(encoding="utf-8")) if ppath.exists() else {
        "name": args.profile, "calibrated": False, "analysis_width": 640, "analysis_height": 360,
        "markers": [], "encounters": [], "context": {}}
    width = int(profile.get("analysis_width", 320))
    x, y, w, h = args.roi

    def crop_at(t):
        g = cv2.cvtColor(grab(info, parse_time(t), width), cv2.COLOR_RGB2GRAY)
        H, W = g.shape
        return g[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]

    crop = crop_at(args.t)
    if crop.size == 0 or crop.std() < 3:
        raise SystemExit("Crop is empty or flat - check --t and --roi with the 'frame' command first")
    # Mask = the banner's own pixels, so the changing scene behind it doesn't spoil the match.
    if args.also_t:
        # best: pixels that stay the same across several sightings of the same banner
        mask = np.ones(crop.shape, bool)
        for t2 in args.also_t:
            mask &= np.abs(crop.astype(np.int16) - crop_at(t2).astype(np.int16)) < 18
        how = f"stable across {1 + len(args.also_t)} sightings"
    else:
        # fallback: pixels that stand out from the crop's typical value (text, bar fill, frame)
        med = np.median(crop)
        mask = np.abs(crop.astype(np.float32) - med) > max(25.0, 0.8 * crop.std())
        how = "foreground estimate (pass --also-t for a better mask)"
    mask = cv2.morphologyEx(mask.astype(np.uint8) * 255, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    cover = (mask > 0).mean()
    if cover < 0.03:
        raise SystemExit(f"Mask covers only {cover:.0%} of the crop - wrong timestamps or ROI?")
    tpl_rel, mask_rel = f"templates/{args.id}.png", f"templates/{args.id}_mask.png"
    cv2.imwrite(str(pdir / tpl_rel), crop)
    cv2.imwrite(str(pdir / mask_rel), mask)
    print(f"Mask: {cover:.0%} of the crop, {how}")
    p = args.pad
    roi = [max(0.0, x - p), max(0.0, y - p), min(1.0 - max(0.0, x - p), w + 2 * p),
           min(1.0 - max(0.0, y - p), h + 2 * p)]
    marker = {"id": args.id, "event": args.event, "template": tpl_rel, "mask": mask_rel, "ref_width": width,
              "roi": [round(v, 4) for v in roi], "threshold": args.threshold,
              "min_duration_s": args.min_duration, "source": f"{Path(args.video).name} @ {args.t}"}
    profile["markers"] = [m for m in profile.get("markers", []) if m["id"] != args.id] + [marker]
    ppath.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print(f"Saved template {pdir / tpl_rel} ({crop.shape[1]}x{crop.shape[0]}) and updated {ppath}")
    print("Next: run detect.py with --profile on a recording you did NOT build the template from.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("frame"); a.add_argument("video"); a.add_argument("--t", required=True)
    a.add_argument("--width", type=int, default=1280)
    b = sub.add_parser("crop"); b.add_argument("video"); b.add_argument("--t", required=True)
    b.add_argument("--roi", type=float, nargs=4, required=True, metavar=("X", "Y", "W", "H"))
    b.add_argument("--also-t", action="append", default=[],
                   help="another time the same banner is on screen (repeatable) - gives a much better mask")
    b.add_argument("--profile", required=True); b.add_argument("--id", required=True)
    b.add_argument("--event", required=True)
    b.add_argument("--pad", type=float, default=0.04)
    b.add_argument("--threshold", type=float, default=0.7)
    b.add_argument("--min-duration", type=float, default=0.6)
    args = ap.parse_args()
    {"frame": cmd_frame, "crop": cmd_crop}[args.cmd](args)


if __name__ == "__main__":
    main()
