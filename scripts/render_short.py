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


def speed_filters(seg: dict) -> tuple[str, str]:
    """Speed ramp (borrowed from Clipify): 'speed': 2.0 plays the segment twice as fast instead of cutting it.
    Returns (video prefix, audio filter). Output time = source time / speed."""
    sp = float(seg.get("speed", 1.0))
    if abs(sp - 1.0) < 1e-3:
        return "", ""
    tempo, s = [], sp
    while s > 2.0:            # atempo accepts 0.5-2.0 per instance
        tempo.append("atempo=2.0")
        s /= 2.0
    tempo.append(f"atempo={s:.4f}")
    return f"setpts=PTS/{sp},", ",".join(tempo) + ","


def segment_graph(seg: dict, info, dur: float, workdir: Path, idx: int, encoder: str,
                  sub_f: str = "") -> tuple[str, list[str]]:
    """Returns (filter graph, extra input args for caption images)."""
    from captions import overlay_graph
    pre = []
    if info.height > 1080 and info.height - 1080 <= 16:      # PS5 VP9 is 1920x1088: drop the padding
        pre.append(f"crop={info.width}:1080:0:0")
    tm = tonemap_chain(info)
    if tm:
        pre.append(tm + ",format=yuv420p")
    if seg.get("gamma") or seg.get("brightness"):
        # lift dark (night/interior) footage so it reads on a phone: "gamma": 1.4, optional "brightness": 0.03
        pre.append(f"eq=gamma={float(seg.get('gamma', 1.0))}:brightness={float(seg.get('brightness', 0.0))}"
                   f":saturation=1.08")
    sp_v, sp_a = speed_filters(seg)
    pre_s = sp_v + ((",".join(pre) + ",") if pre else "")
    last_fmt = "nv12" if encoder == "qsv" else "yuv420p"
    fin, fout = seg.get("fade_in", 0.2), seg.get("fade_out", 0.25)
    # Shorts safe zone: keep captions 70 px from the sides (YouTube's buttons sit on the right)
    cap_args, cap_graph, out = overlay_graph(seg.get("texts", []), workdir, f"s{idx}", "base", 1, W, 70)
    if seg.get("layout") == "crop":
        # full-screen 9:16 cut from the source, positioned on the action (crop_x: centre or keyframes)
        src_h = 1080 if (info.height > 1080 and info.height - 1080 <= 16) else info.height
        if info.height >= 2160:
            src_h = 2160
        cw = int(src_h * 9 / 16) // 2 * 2
        cx = crop_x_expr(seg.get("crop_x", 0.5), info.width, cw)
        video = (f"[0:v]{pre_s}crop={cw}:{src_h}:x='{cx}':y=0,scale={W}:{H}:flags=lanczos,"
                 f"fps={FPS},{zoom_filter(seg.get('zoom'), W, H, FPS)}setsar=1,format=yuv420p[base];")
    elif seg.get("blur_only"):
        # title moment: only the blurred, darkened background (text sits centred on it), then the next
        # segment cuts to the sharp gameplay window - reads like a focus pull into the video
        video = (f"[0:v]{pre_s}scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                 f"gblur=sigma=40,eq=brightness=-0.16:saturation=1.15,fps={FPS},"
                 f"setsar=1,format=yuv420p[base];")
    else:
        video = (f"[0:v]{pre_s}split=2[bgs][fgs];"
                 f"[bgs]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                 f"gblur=sigma=40,eq=brightness=-0.10:saturation=1.15[bg];"
                 # fg_scale > 1 enlarges the gameplay window (sides trimmed) so it reads better on a phone
                 f"[fgs]scale={int(W * float(seg.get('fg_scale', 1.0))) // 2 * 2}:-2:flags=lanczos,"
                 f"crop='min(iw,{W})':ih[fg];"
                 f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={FPS},{zoom_filter(seg.get('zoom'), W, H, FPS)}"
                 f"setsar=1,format=yuv420p[base];")
    graph = (
        f"{video}"
        f"{cap_graph}"
        # fade d=0 would fall back to FFmpeg's default 25-frame fade, so omit it instead
        f"[{out}]{sub_f}{f'fade=t=in:st=0:d={fin},' if fin > 0 else ''}"
        f"{f'fade=t=out:st={max(0, dur - fout):.3f}:d={fout},' if fout > 0 else ''}format={last_fmt}[v];"
        f"[0:a]{sp_a}aresample=48000,volume={float(seg.get('game_audio', 1.0))},"
        f"afade=t=in:st=0:d=0.08,afade=t=out:st={max(0, dur - 0.3):.3f}:d=0.3[a]"
    )
    return graph, cap_args


BITRATE: str | None = None  # e.g. "45M" for 4K30 uploads (YouTube recommends 35-45 Mbps); None = quality mode


def code_fingerprint() -> str:
    """Part of every segment cache key: a fix in the rendering code must invalidate cached segments
    (a stale cached title card once survived a bug fix)."""
    import hashlib
    h = hashlib.sha256()
    for name in ("render_short.py", "render_long.py", "captions.py", "gc_common.py", "subtitles.py"):
        p = Path(__file__).with_name(name)
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:10]
SDR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]


def venc(encoder: str) -> list[str]:
    if BITRATE:
        mbps = float(BITRATE.rstrip("Mm"))
        rate = ["-b:v", BITRATE, "-maxrate", f"{mbps * 1.3:.0f}M", "-bufsize", f"{mbps * 2:.0f}M"]
    else:
        rate = None
    if encoder == "qsv":
        return ["-c:v", "h264_qsv", "-preset", "slow", *(rate or ["-global_quality", "20"]), "-profile:v", "high",
                *SDR_TAGS]
    return ["-c:v", "libx264", "-preset", "medium", *(rate or ["-crf", "18"]), "-profile:v", "high",
            "-pix_fmt", "yuv420p", *SDR_TAGS]


def zoom_filter(zooms: list[dict] | None, w: int, h: int, fps: int) -> str:
    """Punch-in emphasis (borrowed from Eklipse-style effects): "zoom": [{"at": 4.0, "dur": 1.5, "scale": 1.15,
    "ramp": 0.2}] - times in segment output seconds. Ramps in, holds, ramps out. Returns 'zoompan=...,' or ''."""
    if not zooms:
        return ""
    terms = []
    for z in zooms:
        a, d, s, r = float(z["at"]), float(z.get("dur", 1.5)), float(z.get("scale", 1.15)), float(z.get("ramp", 0.2))
        terms.append(f"{s - 1}*max(0\\,min(1\\,(it-{a})/{r}))*max(0\\,min(1\\,({a + d}-it)/{r}))")
    expr = "1+" + "+".join(terms)
    return (f"zoompan=z='{expr}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps},")


def crop_x_expr(keys, src_w: int, crop_w: int) -> str:
    """Horizontal crop position: a fixed centre (0-1 of the frame width) or keyframes [[t, centre], ...]
    linearly interpolated over the segment's time t. Clamped so the crop never leaves the frame."""
    def px(c):
        return f"min(max({c}*{src_w}-{crop_w}/2\\,0)\\,{src_w - crop_w})"
    if not isinstance(keys, list):
        return px(float(keys))
    keys = sorted(keys)
    expr = px(keys[-1][1])
    for (t0, c0), (t1, c1) in reversed(list(zip(keys, keys[1:]))):
        c = f"({c0}+({c1}-{c0})*(t-{t0})/{max(1e-3, t1 - t0)})"
        expr = f"if(lt(t\\,{t1})\\,{px(c)}\\,{expr})"
    return f"if(lt(t\\,{keys[0][0]})\\,{px(keys[0][1])}\\,{expr})"


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
    global FPS, BITRATE  # fps: follow the source unless set (GTA 6 renders 30 fps even in a 60 fps capture)
    FPS = int(edl.get("fps") or min(60, round(probe(ROOT / edl["segments"][0]["src"]).fps)) or 60)
    BITRATE = edl.get("bitrate")  # e.g. "12M" for 1080p (YouTube: 8 Mbps @30, 12 Mbps @60)
    outdir = a.edl.resolve().parent
    work = outdir / "work"
    work.mkdir(parents=True, exist_ok=True)
    out = ROOT / edl["output"]
    ff = ffbin("ffmpeg")
    t0 = time.perf_counter()
    with open(outdir / "render_log.txt", "w", encoding="utf-8") as log:
        log.write(f"EDL {a.edl}\nencoder {encoder}\n")
        import narration
        import subtitles
        # timeline + narration first: word timings drive each segment's burned-in subtitles
        seg_starts, total = [], 0.0
        for seg in edl["segments"]:
            seg_starts.append(total)
            total += (float(seg["out"]) - float(seg["in"])) / float(seg.get("speed", 1.0))
        nar_warnings, nar_wav, words = [], None, []
        if edl.get("narration"):
            nar_wav, nar_warnings, words = narration.build(edl["narration"], total, seg_starts, work,
                                                           edl["segments"])
        sub_opts = edl.get("subtitles", {})
        use_subs = bool(words) and sub_opts.get("enabled", True)
        parts = []
        for i, seg in enumerate(edl["segments"]):
            src = ROOT / seg["src"]
            info = probe(src)
            src_dur = float(seg["out"]) - float(seg["in"])
            dur = src_dur / float(seg.get("speed", 1.0))  # output length (speed ramps shorten it)
            ass = (subtitles.segment_ass(words, seg_starts[i], seg_starts[i] + dur, W, H, sub_opts,
                                         work / f"seg{i:02d}.ass") if use_subs else None)
            import hashlib  # incremental renders: re-encode a segment only if its spec/settings changed
            key = hashlib.sha256(json.dumps([seg, W, H, FPS, encoder, BITRATE, code_fingerprint(),
                                             ass.read_text(encoding="utf-8") if ass else ""], sort_keys=True)
                                 .encode()).hexdigest()[:12]
            part = work / f"seg{i:02d}_{key}.mp4"
            if part.exists() and part.stat().st_size > 0:
                parts.append(part)
                print(f"  segment {i + 1}/{len(edl['segments'])}: cached")
                continue
            graph, cap_args = segment_graph(seg, info, dur, work, i, encoder, subtitles.filter_for(ass))
            gfile = work / f"seg{i:02d}.filter"
            gfile.write_text(graph, encoding="utf-8")
            run([ff, "-y", "-hide_banner", "-ss", f"{seg['in']:.3f}", "-t", f"{src_dur:.3f}", "-i", str(src),
                 *cap_args, "-/filter_complex", str(gfile), "-map", "[v]", "-map", "[a]", *venc(encoder),
                 "-r", str(FPS), "-g", str(FPS * 2), "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                 "-t", f"{dur:.3f}", str(part)], log)
            parts.append(part)
            print(f"  segment {i + 1}/{len(edl['segments'])}: {Path(seg['src']).name} {seg['in']}-{seg['out']}s")
        lst = work / "concat.txt"
        lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
        joined = work / "joined.mp4"
        run([ff, "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)], log)
        out.parent.mkdir(parents=True, exist_ok=True)
        # loudness to the YouTube reference (-14 LUFS), true peak below -1.5 dB; video untouched
        sfx_wav = narration.build_sfx(edl.get("sfx", []), total, seg_starts, work)
        mastered = narration.master(ff, run, log, joined, nar_wav, sfx_wav, work)  # mix + two-pass loudness
        run([ff, "-y", "-hide_banner", "-i", str(joined), "-i", str(mastered), "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", "-movflags", "+faststart",
             str(out)], log)
        render_s = time.perf_counter() - t0
        report = qc(out, total, log)
        report.update({"encoder": "h264_qsv (Intel Quick Sync)" if encoder == "qsv" else "libx264 (CPU)",
                       "render_seconds": round(render_s, 1), "segments": len(parts),
                       "narration_lines": len(edl.get("narration", {}).get("lines", []))})
        if nar_warnings:
            report["warnings"] = nar_warnings
        if edl.get("thumbnail_t") is not None:
            thumb = outdir / "thumbnail_frame.jpg"
            run([ff, "-y", "-hide_banner", "-ss", str(edl["thumbnail_t"]), "-i", str(out), "-frames:v", "1",
                 "-q:v", "2", str(thumb)], log)
            report["thumbnail_frame"] = str(thumb.relative_to(ROOT))
    (outdir / "output_metadata.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
