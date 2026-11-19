r"""Render a 16:9 long-form video (1920x1080, 60 fps) from an edit decision list.

  python scripts/render_long.py exports\long\<project>\edl.json [--encoder qsv|x264]

EDL segments, in order:
  {"type": "card", "dur": 3, "lines": ["CHAPTER 1", "LORETTA"], "src": "input/x.mp4", "t": 4.0, "chapter": "..."}
      title card over a blurred, darkened still taken from src at time t (silent)
  {"src": "input/x.mp4", "in": 0.0, "out": 10.8, "chapter": "...", "texts": [ {lines,start,end,y,size,font,color} ]}
      gameplay, full frame (HDR tone-mapped, PS5 1088 padding cropped)

Optional narration, placed on the final timeline and mixed over game audio with automatic ducking:
  "narration": {"voice": "am_michael", "speed": 1.0, "lines": [{"at": 1.0, "text": "..."}]}
  ElevenLabs instead of Kokoro (needs ELEVENLABS_API_KEY in .env; lines are cached):
  "narration": {"engine": "elevenlabs", "voice": "Adam",
                "elevenlabs": {"model": "eleven_multilingual_v2", "stability": 0.35, "style": 0.45}, "lines": [...]}

Outputs (EDL folder): the mp4 (with embedded chapters), chapters.txt (paste into the YouTube description),
render_log.txt, output_metadata.json.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from gc_common import ROOT, ffbin, probe, tonemap_chain
import narration
import subtitles
from captions import overlay_graph
from render_short import qc, run, venc

W, H, FPS = 1920, 1080, 60


def fmt_chapter(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def pre_chain(info) -> str:
    pre = []
    if info.height > 1080 and info.height - 1080 <= 16:
        pre.append(f"crop={info.width}:1080:0:0")
    tm = tonemap_chain(info)
    if tm:
        pre.append(tm + ",format=yuv420p")
    return (",".join(pre) + ",") if pre else ""


def scaled(texts: list[dict], s: float) -> list[dict]:
    """Caption specs are authored on a 1920x1080 canvas; scale them to the output resolution."""
    return [{**t, "y": int(t["y"] * s), "size": int(t.get("size", 72) * s)} for t in texts]


def seg_duration(seg: dict) -> float:
    if seg.get("type") == "card":
        return float(seg["dur"])
    return (float(seg["out"]) - float(seg["in"])) / float(seg.get("speed", 1.0))


def render_segment(seg: dict, i: int, work: Path, encoder: str, log, ass: Path | None = None) -> tuple[Path, float]:
    import hashlib
    import render_short
    ff = ffbin("ffmpeg")
    last_fmt = "nv12" if encoder == "qsv" else "yuv420p"
    # incremental renders: a segment is re-encoded only if its spec or the output settings changed
    subs = ass.read_text(encoding="utf-8") if ass else ""
    sub_f = subtitles.filter_for(ass)
    key = hashlib.sha256(json.dumps([seg, W, H, FPS, encoder, render_short.BITRATE, render_short.code_fingerprint(), subs],
                                    sort_keys=True).encode()).hexdigest()[:12]
    part = work / f"seg{i:02d}_{key}.mp4"
    if part.exists() and part.stat().st_size > 0:
        return part, seg_duration(seg)
    S = W / 1920

    def fades(fin: float, fout: float, dur: float) -> str:  # d=0 would mean FFmpeg's 25-frame default
        return (f"fade=t=in:st=0:d={fin}," if fin > 0 else "") + \
               (f"fade=t=out:st={max(0, dur - fout):.3f}:d={fout}," if fout > 0 else "")

    if seg.get("type") == "card":
        dur = float(seg["dur"])
        src = ROOT / seg["src"]
        info = probe(src)
        size = int(seg.get("size", 120) * S)
        # first line = small yellow label pill, the rest = big hook text (*word* = yellow highlight)
        texts = [{"lines": [seg["lines"][0]], "style": "label", "start": 0.15, "end": dur,
                  "y": H // 2 - int(size * 1.25), "size": int(size * 0.42)}]
        if len(seg["lines"]) > 1:
            texts.append({"lines": seg["lines"][1:], "style": "hook", "start": 0.0, "end": dur,
                          "y": H // 2 - int(size * 0.45), "size": size})
        cap_args, cap_graph, out = overlay_graph(texts, work, f"c{i}", "base", 2, W, int(160 * S))
        n = int(dur * FPS)
        # freeze one frame, timed by the clock (a frame-count loop runs at the SOURCE fps: 59.94 -> half length)
        graph = (f"[0:v]trim=end_frame=1,loop=loop=-1:size=1:start=0,setpts=N/({FPS}*TB),trim=duration={dur},"
                 f"{pre_chain(info)}scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                 f"gblur=sigma=30,eq=brightness=-0.25,fps={FPS},setsar=1,format=yuv420p[base];{cap_graph}"
                 f"[{out}]{sub_f}{fades(0.3, 0.3, dur)}format={last_fmt}[v]")
        gfile = work / f"seg{i:02d}.filter"
        gfile.write_text(graph, encoding="utf-8")
        run([ff, "-y", "-hide_banner", "-ss", f"{seg.get('t', 0)}", "-i", str(src), "-f", "lavfi", "-t", f"{dur}",
             "-i", "anullsrc=r=48000:cl=stereo", *cap_args, "-/filter_complex", str(gfile), "-map", "[v]", "-map",
             "1:a", "-frames:v", str(n), *venc(encoder), "-r", str(FPS), "-c:a", "aac", "-b:a", "192k",
             "-t", f"{dur}", str(part)], log)
        return part, dur
    src = ROOT / seg["src"]
    info = probe(src)
    src_dur = float(seg["out"]) - float(seg["in"])
    dur = seg_duration(seg)
    sp_v, sp_a = render_short.speed_filters(seg)
    cap_args, cap_graph, out = overlay_graph(scaled(seg.get("texts", []), S), work, f"s{i}", "base", 1, W,
                                             int(160 * S))
    graph = (f"[0:v]{sp_v}{pre_chain(info)}scale={W}:{H}:force_original_aspect_ratio=decrease,"
             f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={FPS},{render_short.zoom_filter(seg.get('zoom'), W, H, FPS)}"
             f"setsar=1,format=yuv420p[base];{cap_graph}"
             f"[{out}]{sub_f}{fades(seg.get('fade_in', 0.3), seg.get('fade_out', 0.3), dur)}format={last_fmt}[v];"
             f"[0:a]{sp_a}aresample=48000,volume={float(seg.get('game_audio', 1.0))},"
             f"afade=t=in:st=0:d=0.1,afade=t=out:st={max(0, dur - 0.3):.3f}:d=0.3[a]")
    extra_in = []
    if seg.get("audio_from"):
        # replace this segment's audio with clean sound from elsewhere (e.g. engine/street ambience from a
        # drive without radio) - keeps the visuals of a clip whose own audio has licensed music
        af = seg["audio_from"]
        a_idx = 1 + len(cap_args) // 4  # caption PNG inputs come as 4 args each (-loop 1 -i png)
        graph = graph.replace("[0:a]", f"[{a_idx}:a]")
        extra_in = ["-ss", f"{float(af['in']):.3f}", "-t", f"{dur:.3f}", "-i", str(ROOT / af["src"])]
        graph = graph.replace(f"[{a_idx}:a]{sp_a}", f"[{a_idx}:a]")  # source audio already plays at 1x
    gfile = work / f"seg{i:02d}.filter"
    gfile.write_text(graph, encoding="utf-8")
    run([ff, "-y", "-hide_banner", "-ss", f"{seg['in']:.3f}", "-t", f"{src_dur:.3f}", "-i", str(src), *cap_args,
         *extra_in, "-/filter_complex", str(gfile), "-map", "[v]", "-map", "[a]", *venc(encoder), "-r", str(FPS),
         "-g", str(FPS * 2), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{dur:.3f}", str(part)], log)
    return part, dur


def normalize(edl: dict) -> dict:
    """Allow 'mm:ss' / 'h:mm:ss' strings for in/out/t (easier to author from timeline sheets)."""
    from gc_common import parse_time
    for seg in edl["segments"]:
        for k in ("in", "out", "t"):
            if isinstance(seg.get(k), str):
                seg[k] = parse_time(seg[k])
        if isinstance(seg.get("audio_from", {}).get("in"), str):
            seg["audio_from"]["in"] = parse_time(seg["audio_from"]["in"])
    return edl


def plan(edl: dict, out: Path) -> None:
    """Dry run: exact timeline, chapters and estimated narration placement. Nothing is rendered."""
    rows, t, starts, chapters = [], 0.0, [], []
    for i, seg in enumerate(edl["segments"]):
        d = seg_duration(seg)
        starts.append(t)
        if seg.get("chapter"):
            chapters.append([t, seg["chapter"]])
        src = Path(seg.get("src", "")).stem[-6:]
        rng = "card" if seg.get("type") == "card" else f"{fmt_chapter(seg['in'])}-{fmt_chapter(seg['out'])}"
        rows.append(f"| {i} | {fmt_chapter(t)} | {d:5.1f}s | {src} | {rng} | {seg.get('speed', 1.0)}x | "
                    f"{'swap' if seg.get('audio_from') else ('mute' if seg.get('game_audio') == 0 else 'game')} | "
                    f"{seg.get('note', '')} |")
        t += d
    if chapters:
        chapters[0][0] = 0.0
    lines, prev_end, warns = [], -1.0, []
    for ln in edl.get("narration", {}).get("lines", []):
        at = float(ln["at"]) + (starts[ln["seg"]] if "seg" in ln else 0.0)
        est = len(ln["text"].split()) / 2.6 + 0.3  # ~2.6 words/s narration pace
        if at < prev_end:
            warns.append(f"possible overlap at {fmt_chapter(at)}: '{ln['text'][:40]}'")
        prev_end = at + est
        lines.append(f"| {fmt_chapter(at)} | ~{est:.1f}s | {ln['text']} |")
    words = sum(len(ln["text"].split()) for ln in edl.get("narration", {}).get("lines", []))
    chars = sum(len(ln["text"]) for ln in edl.get("narration", {}).get("lines", []))
    md = [f"# Timeline: {edl.get('title', '')}", "",
          f"**Total runtime: {fmt_chapter(t)}** ({t:.1f} s) · {len(edl['segments'])} segments · "
          f"narration {words} words / {chars} characters", "",
          "## Chapters (YouTube description)", ""] + [f"{fmt_chapter(c)} {n}" for c, n in chapters] + [
          "", "## Segments", "", "| # | Starts | Length | Rec | Source in-out | Speed | Audio | Note |",
          "|---|---|---|---|---|---|---|---|"] + rows + [
          "", "## Narration placement (estimated durations)", "", "| At | Length | Line |", "|---|---|---|"] + lines
    if warns:
        md += ["", "## Warnings", ""] + [f"- {w}" for w in warns]
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"runtime {fmt_chapter(t)} ({t:.1f}s), {len(chapters)} chapters, {words} narration words -> {out}")
    for w in warns:
        print("  !", w)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("edl", type=Path)
    ap.add_argument("--encoder", choices=["qsv", "x264"])
    ap.add_argument("--plan", action="store_true", help="dry run: write timeline.md (durations, chapters), no render")
    a = ap.parse_args()
    edl = normalize(json.loads(a.edl.read_text(encoding="utf-8-sig")))  # utf-8-sig: tolerate a BOM
    if a.plan:
        plan(edl, a.edl.resolve().parent / "timeline.md")
        return
    encoder = a.encoder or edl.get("encoder", "qsv")
    global FPS, W, H  # fps: follow the source unless set (GTA 6 renders 30 fps even in a 60 fps capture)
    first = next(s for s in edl["segments"] if s.get("src"))
    FPS = int(edl.get("fps") or min(60, round(probe(ROOT / first["src"]).fps)) or 60)
    W, H = edl.get("resolution", [1920, 1080])
    import render_short
    render_short.BITRATE = edl.get("bitrate")  # e.g. "45M" for 4K30 (YouTube: 35-45 Mbps)
    outdir = a.edl.resolve().parent
    work = outdir / "work"
    work.mkdir(parents=True, exist_ok=True)
    out = ROOT / edl["output"]
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = ffbin("ffmpeg")
    t0 = time.perf_counter()
    with open(outdir / "render_log.txt", "w", encoding="utf-8") as log:
        # timeline first, then narration (its word timings drive the burned-in subtitles of each segment)
        seg_starts, t = [], 0.0
        for seg in edl["segments"]:
            seg_starts.append(t)
            t += seg_duration(seg)
        total = t
        nar_warnings, nar_wav, words = [], None, []
        if edl.get("narration"):
            nar_wav, nar_warnings, words = narration.build(edl["narration"], total, seg_starts, work,
                                                           edl["segments"])
        sub_opts = edl.get("subtitles", {})
        use_subs = bool(words) and sub_opts.get("enabled", True)

        parts, chapters = [], []
        for i, seg in enumerate(edl["segments"]):
            d = seg_duration(seg)
            ass = (subtitles.segment_ass(words, seg_starts[i], seg_starts[i] + d, W, H, sub_opts,
                                         work / f"seg{i:02d}.ass") if use_subs else None)
            part, dur = render_segment(seg, i, work, encoder, log, ass)
            if seg.get("chapter"):
                chapters.append((seg_starts[i], seg["chapter"]))
            parts.append(part)
            print(f"  segment {i + 1}/{len(edl['segments'])} ({dur:.1f}s) {seg.get('chapter', '')}")
        lst = work / "concat.txt"
        lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
        joined = work / "joined.mp4"
        run([ff, "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)], log)

        # chapters: YouTube needs the first at 00:00 and each at least 10 s long
        if chapters and chapters[0][0] > 0:
            chapters[0] = (0.0, chapters[0][1])  # intro card belongs to chapter 1
        meta = work / "chapters.ffmeta"
        lines = [";FFMETADATA1", f"title={edl.get('title', '')}"]
        for k, (cs, name) in enumerate(chapters):
            ce = chapters[k + 1][0] if k + 1 < len(chapters) else total
            lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(cs * 1000)}", f"END={int(ce * 1000)}", f"title={name}"]
        meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
        (outdir / "chapters.txt").write_text("".join(f"{fmt_chapter(cs)} {n}\n" for cs, n in chapters), encoding="utf-8")

        sfx_wav = narration.build_sfx(edl.get("sfx", []), total, seg_starts, work)
        mastered = narration.master(ff, run, log, joined, nar_wav, sfx_wav, work)  # mix + two-pass loudness
        run([ff, "-y", "-hide_banner", "-i", str(joined), "-i", str(mastered), "-i", str(meta),
             "-map", "0:v", "-map", "1:a", "-map_metadata", "2", "-map_chapters", "2", "-c:v", "copy",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", "-movflags", "+faststart", str(out)], log)
        report = qc(out, total, log, size=(W, H))
        report.update({"encoder": "h264_qsv (Intel Quick Sync)" if encoder == "qsv" else "libx264 (CPU)",
                       "render_seconds": round(time.perf_counter() - t0, 1), "segments": len(parts),
                       "chapters": [f"{fmt_chapter(cs)} {n}" for cs, n in chapters],
                       "narration_lines": len(edl.get("narration", {}).get("lines", []))})
        short_ch = [n for k, (cs, n) in enumerate(chapters)
                    if ((chapters[k + 1][0] if k + 1 < len(chapters) else total) - cs) < 10]
        if short_ch:
            nar_warnings.append(f"YouTube ignores chapters under 10 s: {short_ch}")
        if nar_warnings:
            report["warnings"] = nar_warnings
    (outdir / "output_metadata.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
