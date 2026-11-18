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


def render_segment(seg: dict, i: int, work: Path, encoder: str, log) -> tuple[Path, float]:
    ff = ffbin("ffmpeg")
    last_fmt = "nv12" if encoder == "qsv" else "yuv420p"
    part = work / f"seg{i:02d}.mp4"

    def fades(fin: float, fout: float, dur: float) -> str:  # d=0 would mean FFmpeg's 25-frame default
        return (f"fade=t=in:st=0:d={fin}," if fin > 0 else "") + \
               (f"fade=t=out:st={max(0, dur - fout):.3f}:d={fout}," if fout > 0 else "")

    if seg.get("type") == "card":
        dur = float(seg["dur"])
        src = ROOT / seg["src"]
        info = probe(src)
        size = seg.get("size", 120)
        # first line = small yellow label pill, the rest = big hook text (*word* = yellow highlight)
        texts = [{"lines": [seg["lines"][0]], "style": "label", "start": 0.15, "end": dur,
                  "y": H // 2 - int(size * 1.25), "size": int(size * 0.42)}]
        if len(seg["lines"]) > 1:
            texts.append({"lines": seg["lines"][1:], "style": "hook", "start": 0.0, "end": dur,
                          "y": H // 2 - int(size * 0.45), "size": size})
        cap_args, cap_graph, out = overlay_graph(texts, work, f"c{i}", "base", 2, W, 160)
        n = int(dur * FPS)
        graph = (f"[0:v]trim=end_frame=1,setpts=PTS-STARTPTS,loop=loop={n}:size=1:start=0,"
                 f"{pre_chain(info)}scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                 f"gblur=sigma=30,eq=brightness=-0.25,fps={FPS},setsar=1,format=yuv420p[base];{cap_graph}"
                 f"[{out}]{fades(0.3, 0.3, dur)}format={last_fmt}[v]")
        gfile = work / f"seg{i:02d}.filter"
        gfile.write_text(graph, encoding="utf-8")
        run([ff, "-y", "-hide_banner", "-ss", f"{seg.get('t', 0)}", "-i", str(src), "-f", "lavfi", "-t", f"{dur}",
             "-i", "anullsrc=r=48000:cl=stereo", *cap_args, "-/filter_complex", str(gfile), "-map", "[v]", "-map",
             "1:a", "-frames:v", str(n), *venc(encoder), "-r", str(FPS), "-c:a", "aac", "-b:a", "192k",
             "-t", f"{dur}", str(part)], log)
        return part, dur
    src = ROOT / seg["src"]
    info = probe(src)
    dur = float(seg["out"]) - float(seg["in"])
    cap_args, cap_graph, out = overlay_graph(seg.get("texts", []), work, f"s{i}", "base", 1, W, 160)
    graph = (f"[0:v]{pre_chain(info)}scale={W}:{H}:force_original_aspect_ratio=decrease,"
             f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={FPS},setsar=1,format=yuv420p[base];{cap_graph}"
             f"[{out}]{fades(seg.get('fade_in', 0.3), seg.get('fade_out', 0.3), dur)}format={last_fmt}[v];"
             f"[0:a]aresample=48000,afade=t=in:st=0:d=0.1,afade=t=out:st={max(0, dur - 0.3):.3f}:d=0.3[a]")
    gfile = work / f"seg{i:02d}.filter"
    gfile.write_text(graph, encoding="utf-8")
    run([ff, "-y", "-hide_banner", "-ss", f"{seg['in']:.3f}", "-t", f"{dur:.3f}", "-i", str(src), *cap_args,
         "-/filter_complex", str(gfile), "-map", "[v]", "-map", "[a]", *venc(encoder), "-r", str(FPS),
         "-g", str(FPS * 2), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(part)], log)
    return part, dur


def build_narration(nar: dict, total: float, work: Path) -> Path:
    """One WAV the length of the video with every line placed at its 'at' time."""
    from scipy.io import wavfile
    import tts
    name = nar.get("engine", "kokoro")
    k = tts.engine(name, **nar.get("elevenlabs", {})) if name == "elevenlabs" else tts.engine()
    voice = nar.get("voice", tts.DEFAULT_VOICE[name])
    sr = 24000
    track = np.zeros(int((total + 1) * sr), np.float32)
    for ln in nar["lines"]:
        audio, sr_ = tts.speak(k, ln["text"], voice, nar.get("speed", 1.0))
        s = int(ln["at"] * sr)
        e = min(len(track), s + len(audio))
        track[s:e] += audio[: e - s]
        ln["ends_at"] = round(ln["at"] + len(audio) / sr, 2)
    path = work / "narration.wav"
    wavfile.write(path, sr, (np.clip(track, -1, 1) * 32767).astype(np.int16))
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("edl", type=Path)
    ap.add_argument("--encoder", choices=["qsv", "x264"])
    a = ap.parse_args()
    edl = json.loads(a.edl.read_text(encoding="utf-8-sig"))  # tolerate a BOM (PowerShell 5 writes one)
    encoder = a.encoder or edl.get("encoder", "qsv")
    global FPS  # follow the source (GTA 6 is 30 fps; converting to 60 only duplicates frames)
    first = next(s for s in edl["segments"] if s.get("src"))
    FPS = int(edl.get("fps") or min(60, round(probe(ROOT / first["src"]).fps)) or 60)
    outdir = a.edl.resolve().parent
    work = outdir / "work"
    work.mkdir(parents=True, exist_ok=True)
    out = ROOT / edl["output"]
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = ffbin("ffmpeg")
    t0 = time.perf_counter()
    with open(outdir / "render_log.txt", "w", encoding="utf-8") as log:
        parts, chapters, t = [], [], 0.0
        for i, seg in enumerate(edl["segments"]):
            part, dur = render_segment(seg, i, work, encoder, log)
            if seg.get("chapter"):
                chapters.append((t, seg["chapter"]))
            parts.append(part)
            t += dur
            print(f"  segment {i + 1}/{len(edl['segments'])} ({dur:.1f}s) {seg.get('chapter', '')}")
        total = t
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

        inputs = [ff, "-y", "-hide_banner", "-i", str(joined), "-i", str(meta)]
        if edl.get("narration"):
            nar_wav = build_narration(edl["narration"], total, work)
            inputs += ["-i", str(nar_wav)]
            # game audio ducks ~10 dB under the narrator, then everything to -14 LUFS
            afilter = ("[2:a]aresample=48000,volume=1.6,asplit=2[nar][key];"
                       "[0:a][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[game];"
                       "[game][nar]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[a]")
        else:
            afilter = "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]"
        run(inputs + ["-filter_complex", afilter, "-map", "0:v", "-map", "[a]", "-map_metadata", "1",
                      "-map_chapters", "1", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                      "-movflags", "+faststart", str(out)], log)
        report = qc(out, total, log, size=(W, H))
        report.update({"encoder": "h264_qsv (Intel Quick Sync)" if encoder == "qsv" else "libx264 (CPU)",
                       "render_seconds": round(time.perf_counter() - t0, 1), "segments": len(parts),
                       "chapters": [f"{fmt_chapter(cs)} {n}" for cs, n in chapters],
                       "narration_lines": len(edl.get("narration", {}).get("lines", []))})
        short_ch = [n for k, (cs, n) in enumerate(chapters)
                    if ((chapters[k + 1][0] if k + 1 < len(chapters) else total) - cs) < 10]
        if short_ch:
            report["warning"] = f"YouTube ignores chapters under 10 s: {short_ch}"
    (outdir / "output_metadata.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
