"""Inspect PS5 recordings and (optionally) benchmark CPU vs hardware decoding.

Usage:
  python scripts/probe.py                 # every video in input/
  python scripts/probe.py path\to\file.mp4
  python scripts/probe.py --bench 60      # also time 60 s of analysis decoding per method
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from gc_common import ANALYSIS_DIR, INPUT_DIR, iter_frames, list_videos, load_config, probe

METHODS = ["none", "qsv", "d3d11va"]


def bench(info, seconds: float, cfg: dict) -> dict:
    a = cfg["analysis"]
    secs = min(seconds, info.duration)
    results = {}
    for m in METHODS:
        t0 = time.perf_counter()
        n = 0
        try:
            for _ in iter_frames(info, a["width"], a["height"], a["fps"], hwaccel=m, duration=secs):
                n += 1
            dt = time.perf_counter() - t0
            ok = n >= int(secs * a["fps"] * 0.9)
            results[m] = {"ok": ok, "frames": n, "seconds": round(dt, 2),
                          "x_realtime": round(secs / dt, 2) if dt else None}
        except Exception as e:
            results[m] = {"ok": False, "error": str(e)[:200]}
    good = {k: v for k, v in results.items() if v.get("ok")}
    best = max(good, key=lambda k: good[k]["x_realtime"]) if good else None
    return {"seconds_tested": secs, "methods": results, "fastest": best}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default=str(INPUT_DIR))
    ap.add_argument("--bench", type=float, default=0, help="seconds of video to decode per method")
    args = ap.parse_args()

    cfg = load_config()
    videos = list_videos(Path(args.target))
    if not videos:
        print(f"No videos found in {args.target}")
        return
    ANALYSIS_DIR.mkdir(exist_ok=True)
    report = []
    for v in videos:
        info = probe(v)
        s = info.summary()
        if args.bench:
            s["decode_benchmark"] = bench(info, args.bench, cfg)
        report.append(s)
        print(json.dumps(s, indent=2))
    out = ANALYSIS_DIR / "probe_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
