r"""Render a vertical 1080x1920 Short from an edit decision list (EDL).

  python scripts/render_short.py exports\shorts\two-worlds\edl.json [--encoder qsv|x264]

Layout: full landscape frame (nothing cropped away) on a blurred, darkened copy of itself,
text above/below the gameplay. Original files are only read. Output + log + QC report go to
the EDL's folder.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from gc_common import ROOT, ffbin, probe, tonemap_chain

W, H, FPS = 1080, 1920, 60
FONTS = {
    "impact": "C:/Windows/Fonts/impact.ttf",
    "bahn": "C:/Windows/Fonts/bahnschrift.ttf",
    "black": "C:/Windows/Fonts/ariblk.ttf",
}


def fpath(p: Path | str) -> str:
    """Path inside a filtergraph: forward slashes, escaped drive colon."""
    return str(p).replace("\\", "/").replace(":", "\\:")


def drawtexts(spec: dict, workdir: Path, key: str) -> list[str]:
    out = []
    a, b, fade = spec["start"], spec["end"], 0.25
    alpha = f"if(lt(t,{a}+{fade}),(t-{a})/{fade},if(gt(t,{b}-{fade}),({b}-t)/{fade},1))"
    size = spec.get("size", 72)
    for i, line in enumerate(spec["lines"]):
        tf = workdir / f"{key}_{i}.txt"
        tf.write_text(line, encoding="utf-8")
        y = spec["y"] + int(i * size * 1.18)
        out.append(
            f"drawtext=fontfile='{fpath(FONTS[spec.get('font', 'impact')])}':textfile='{fpath(tf)}'"
            f":fontsize={size}:fontcolor={spec.get('color', 'white')}:x=(w-text_w)/2:y={y}"
            f":borderw={spec.get('border', 5)}:bordercolor=black@0.85"
            f":enable='between(t,{a},{b})':alpha='{alpha}'")
    return out


def segment_graph(seg: dict, info, dur: float, workdir: Path, idx: int, encoder: str) -> tuple[str, list[str]]:
    """Returns (filter graph, extra input args for caption images)."""
    from captions import overlay_graph
    pre = []
    if info.height > 1080 and info.height - 1080 <= 16:      # PS5 VP9 is 1920x1088: drop the padding
        pre.append(f"crop={info.width}:1080:0:0")
    tm = tonemap_chain(info)
    if tm:
        pre.append(tm + ",format=yuv420p")
    pre_s = (",".join(pre) + ",") if pre else ""
    last_fmt = "nv12" if encoder == "qsv" else "yuv420p"
    fin, fout = seg.get("fade_in", 0.2), seg.get("fade_out", 0.25)
    # Shorts safe zone: keep captions 70 px from the sides (YouTube's buttons sit on the right)
    cap_args, cap_graph, out = overlay_graph(seg.get("texts", []), workdir, f"s{idx}", "base", 1, W, 70)
    graph = (
        f"[0:v]{pre_s}split=2[bgs][fgs];"
        f"[bgs]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"gblur=sigma=40,eq=brightness=-0.10:saturation=1.15[bg];"
        f"[fgs]scale={W}:-2:flags=lanczos[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={FPS},setsar=1,format=yuv420p[base];"
        f"{cap_graph}"
        # fade d=0 would fall back to FFmpeg's default 25-frame fade, so omit it instead
        f"[{out}]{f'fade=t=in:st=0:d={fin},' if fin > 0 else ''}"
        f"{f'fade=t=out:st={max(0, dur - fout):.3f}:d={fout},' if fout > 0 else ''}format={last_fmt}[v];"
        f"[0:a]aresample=48000,afade=t=in:st=0:d=0.08,afade=t=out:st={max(0, dur - 0.3):.3f}:d=0.3[a]"
    )
    return graph, cap_args


def venc(encoder: str) -> list[str]:
    if encoder == "qsv":
        return ["-c:v", "h264_qsv", "-preset", "slow", "-global_quality", "20", "-profile:v", "high"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high", "-pix_fmt", "yuv420p"]


def run(cmd: list[str], log) -> str:
    log.write("\n$ " + " ".join(cmd) + "\n")
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    log.write(p.stderr[-4000:])
    if p.returncode != 0:
        raise SystemExit(f"ffmpeg failed (exit {p.returncode}) - see render_log.txt\n{p.stderr[-1500:]}")
    return p.stderr


def qc(out: Path, expected: float, log, size: tuple[int, int] = (W, H)) -> dict:
    info = probe(out)
    ff = ffbin("ffmpeg")
    black = run([ff, "-hide_banner", "-nostats", "-i", str(out), "-vf", "blackdetect=d=0.4:pix_th=0.08",
                 "-an", "-f", "null", "-"], log)
    loud = run([ff, "-hide_banner", "-nostats", "-i", str(out), "-af", "ebur128=peak=true", "-vn", "-f", "null", "-"], log)
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", black)]
    m_i = re.findall(r"I:\s+(-?[\d.]+) LUFS", loud)
    m_tp = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", loud)
    report = {
        "file": str(out.relative_to(ROOT)),
        "duration_s": round(info.duration, 2), "expected_s": round(expected, 2),
        "video": info.summary()["video"], "audio": info.summary()["audio"],
        "size_mb": round(out.stat().st_size / 1e6, 1),
        "integrated_loudness_lufs": float(m_i[-1]) if m_i else None,
        "true_peak_dbfs": float(m_tp[-1]) if m_tp else None,
        "black_intervals_s": blacks,
    }
    ok = (info.width, info.height) == size and abs(info.duration - expected) < 0.5 and info.acodec == "aac"
    report["checks_passed"] = ok
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("edl", type=Path)
    ap.add_argument("--encoder", choices=["qsv", "x264"])
    a = ap.parse_args()
    edl = json.loads(a.edl.read_text(encoding="utf-8-sig"))  # tolerate a BOM (PowerShell 5 writes one)
    encoder = a.encoder or edl.get("encoder", "qsv")
    global FPS  # follow the source (GTA 6 is 30 fps; converting to 60 only duplicates frames)
    FPS = int(edl.get("fps") or min(60, round(probe(ROOT / edl["segments"][0]["src"]).fps)) or 60)
    outdir = a.edl.resolve().parent
    work = outdir / "work"
    work.mkdir(parents=True, exist_ok=True)
    out = ROOT / edl["output"]
    ff = ffbin("ffmpeg")
    t0 = time.perf_counter()
    with open(outdir / "render_log.txt", "w", encoding="utf-8") as log:
        log.write(f"EDL {a.edl}\nencoder {encoder}\n")
        parts, total = [], 0.0
        for i, seg in enumerate(edl["segments"]):
            src = ROOT / seg["src"]
            info = probe(src)
            dur = float(seg["out"]) - float(seg["in"])
            total += dur
            graph, cap_args = segment_graph(seg, info, dur, work, i, encoder)
            gfile = work / f"seg{i:02d}.filter"
            gfile.write_text(graph, encoding="utf-8")
            part = work / f"seg{i:02d}.mp4"
            run([ff, "-y", "-hide_banner", "-ss", f"{seg['in']:.3f}", "-t", f"{dur:.3f}", "-i", str(src), *cap_args,
                 "-/filter_complex", str(gfile), "-map", "[v]", "-map", "[a]", *venc(encoder),
                 "-r", str(FPS), "-g", str(FPS * 2), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(part)], log)
            parts.append(part)
            print(f"  segment {i + 1}/{len(edl['segments'])}: {Path(seg['src']).name} {seg['in']}-{seg['out']}s")
        lst = work / "concat.txt"
        lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
        joined = work / "joined.mp4"
        run([ff, "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)], log)
        out.parent.mkdir(parents=True, exist_ok=True)
        # loudness to the YouTube reference (-14 LUFS), true peak below -1.5 dB; video untouched
        run([ff, "-y", "-hide_banner", "-i", str(joined), "-c:v", "copy",
             "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
             "-movflags", "+faststart", str(out)], log)
        render_s = time.perf_counter() - t0
        report = qc(out, total, log)
        report.update({"encoder": "h264_qsv (Intel Quick Sync)" if encoder == "qsv" else "libx264 (CPU)",
                       "render_seconds": round(render_s, 1), "segments": len(parts)})
        if edl.get("thumbnail_t") is not None:
            thumb = outdir / "thumbnail_frame.jpg"
            run([ff, "-y", "-hide_banner", "-ss", str(edl["thumbnail_t"]), "-i", str(out), "-frames:v", "1",
                 "-q:v", "2", str(thumb)], log)
            report["thumbnail_frame"] = str(thumb.relative_to(ROOT))
    (outdir / "output_metadata.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
