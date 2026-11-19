r"""Generate every narration line of an EDL once (ElevenLabs lines are cached, so the later render costs
nothing extra), MEASURE the real spoken lengths, and check the placement against the actual timeline.

  python scripts/narrate.py exports\long\gta6-first-hours\edl.json
  -> narration_check.md next to the EDL (actual start/end of every line, gaps, overlaps, overruns)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import render_long
import tts
from render_long import fmt_chapter, normalize, seg_duration


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("edl", type=Path)
    a = ap.parse_args()
    edl = normalize(json.loads(a.edl.read_text(encoding="utf-8-sig")))
    starts, t, ends = [], 0.0, []
    for seg in edl["segments"]:
        starts.append(t)
        t += seg_duration(seg)
        ends.append(t)
    total = t
    nar = edl["narration"]
    name = nar.get("engine", "kokoro")
    k = tts.engine(name, **nar.get("elevenlabs", {})) if name == "elevenlabs" else tts.engine()
    voice = nar.get("voice", tts.DEFAULT_VOICE[name])
    rows, placed, chars = [], [], 0
    import narration
    for ln in nar["lines"]:
        at = narration.line_time(ln, starts, edl["segments"])
        audio, sr = tts.speak(k, ln["text"], voice, nar.get("speed", 1.0))
        dur = len(audio) / sr
        chars += len(ln["text"])
        seg_end = ends[ln["seg"]] if "seg" in ln else total
        placed.append((at, at + dur, ln["text"], seg_end))
    placed.sort()
    issues = 0
    for i, (s, e, text, seg_end) in enumerate(placed):
        nxt = placed[i + 1][0] if i + 1 < len(placed) else total
        gap = nxt - e
        flag = ""
        if gap < 0:
            flag = f"OVERLAP {abs(gap):.1f}s"
        elif gap < 0.25:
            flag = "tight"
        if e > total:
            flag = "RUNS PAST END"
        if flag and flag != "tight":
            issues += 1
        spill = " (continues into next segment)" if e > seg_end + 0.05 else ""
        rows.append(f"| {fmt_chapter(s)} ({s:.1f}) | {e - s:.1f}s | {gap:+.1f}s | {flag} | {text}{spill} |")
    out = a.edl.resolve().parent / "narration_check.md"
    out.write_text("\n".join([
        f"# Narration check: measured ({name}, voice {voice})", "",
        f"{len(placed)} lines · {chars} characters · video {fmt_chapter(total)} · **{issues} problem(s)**", "",
        "| Starts | Spoken | Gap to next | Flag | Line |", "|---|---|---|---|---|", *rows]) + "\n", encoding="utf-8")
    print(f"{len(placed)} lines, {chars} chars, {issues} problem(s) -> {out}")


if __name__ == "__main__":
    main()
