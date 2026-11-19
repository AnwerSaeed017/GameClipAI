"""Word-synced narration subtitles (ASS, rendered by libass): short phrases, active word highlighted
with a small pop, bold Montserrat with a dark-purple outline. Built from ElevenLabs word timings.

EDL (optional):
  "subtitles": {"enabled": true, "size": 120, "margin_v": 470, "highlight": "#FF4FA0", "max_words": 4}
  Defaults depend on the canvas (long 16:9 vs Short 9:16).
"""
from __future__ import annotations

import os
from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
# libass/fontconfig: use ONLY the project fonts with a persistent cache (child ffmpeg processes inherit
# this). Otherwise every ffmpeg run rescans all Windows fonts, which costs minutes per segment.
os.environ.setdefault("FONTCONFIG_FILE", str(FONTS_DIR / "fonts.conf"))


def _ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def defaults(w: int, h: int) -> dict:
    if h > w:  # vertical Short: above YouTube's bottom UI, below the gameplay window
        return {"size": int(h * 0.040), "margin_v": int(h * 0.20), "outline": 6, "shadow": 3, "max_words": 3}
    # 16:9: lower third, kept ABOVE the game's own subtitles (which sit at ~86-92 % height)
    return {"size": int(h * 0.056), "margin_v": int(h * 0.20), "outline": max(4, int(h * 0.0042)),
            "shadow": max(2, int(h * 0.002)), "max_words": 4}


def phrases(words: list[tuple[str, float, float]], max_words: int,
            max_chars: int = 999) -> list[list[tuple[str, float, float]]]:
    """Group words into short readable phrases: break on punctuation, pauses, word count and WIDTH
    (max_chars keeps a phrase on one line inside the frame; a phone-width Short fits ~14 chars)."""
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        pause = (nxt[1] - w[2]) if nxt else 9
        too_wide = nxt is not None and len(" ".join(x[0] for x in cur + [nxt])) > max_chars
        if len(cur) >= max_words or too_wide or w[0][-1:] in ".,!?…:;" or pause > 0.35:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _glue(words: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    """Keep names like 'GTA 6' / 'GTA 5' together: never split across phrases, highlighted as one."""
    out = []
    for w in words:
        if out and out[-1][0].upper() == "GTA" and w[0][:1].isdigit():
            out[-1] = (f"{out[-1][0]} {w[0]}", out[-1][1], w[2])
        else:
            out.append(w)
    return out


def build_ass(words: list[tuple[str, float, float]], w: int, h: int, opts: dict | None = None) -> str:
    o = {**defaults(w, h), **(opts or {})}
    hi = _ass_color(o.get("highlight", "#FF4FA0"))
    white, outline, shadow = _ass_color("#FFFFFF"), _ass_color("#19052D"), _ass_color("#000000", 0x80)
    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,Montserrat Black,{o['size']},{white},{white},{outline},{shadow},0,0,0,0,100,100,1,0,1,"
        f"{o['outline']},{o['shadow']},2,{int(w * 0.08)},{int(w * 0.08)},{o['margin_v']},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    ev = []
    words = _glue(words)
    # Montserrat Black caps measured ~0.40 em per character incl. spaces ("THIS IS THE TRUCK" = 773 px at
    # size 116); 0.42 + 6 % for the active-word pop. Usable width = frame minus the 8 % side margins.
    # Anything still too wide wraps (WrapStyle 0) instead of running off the screen.
    max_chars = o.get("max_chars") or int((w * 0.84) / (o["size"] * 0.42 * 1.06))
    ph = phrases(words, int(o["max_words"]), int(max_chars))
    for pi, p in enumerate(ph):
        p_end = p[-1][2] + 0.25
        if pi + 1 < len(ph):
            p_end = min(p_end, ph[pi + 1][0][1])
        for i, (word, s, e) in enumerate(p):
            end = p[i + 1][1] if i + 1 < len(p) else p_end
            parts = []
            for j, (wj, _, _) in enumerate(p):
                txt = wj.upper().replace("{", "(").replace("}", ")")
                if j == i:
                    parts.append(f"{{\\c{hi}\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)}}{txt}{{\\c{white}}}")
                else:
                    parts.append(txt)
            ev.append(f"Dialogue: 0,{_ts(s)},{_ts(max(end, s + 0.05))},Cap,,0,0,0,,{' '.join(parts)}")
    return "\n".join(head + ev) + "\n"


def segment_ass(words, seg_start: float, seg_end: float, w: int, h: int, opts: dict | None,
                path: Path) -> Path | None:
    """ASS for one segment: words overlapping [seg_start, seg_end), times shifted to segment time.
    Phrases are formed on the FULL timeline first, so a phrase crossing a cut stays intact."""
    o = {**defaults(w, h), **(opts or {})}
    full = build_ass(words, w, h, o)
    lines, keep = full.splitlines(), []
    for ln in lines:
        if not ln.startswith("Dialogue:"):
            keep.append(ln)
            continue
        f = ln.split(",", 9)
        s, e = _parse(f[1]), _parse(f[2])
        if e <= seg_start or s >= seg_end:
            continue
        f[1], f[2] = _ts(s - seg_start), _ts(min(e, seg_end) - seg_start)
        keep.append(",".join(f))
    if not any(l.startswith("Dialogue:") for l in keep):
        return None
    path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return path


def _parse(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def filter_for(path: Path | None) -> str:
    if path is None:
        return ""
    esc = lambda p: str(p).replace("\\", "/").replace(":", "\\:")  # noqa: E731
    return f"subtitles=filename='{esc(path)}':fontsdir='{esc(FONTS_DIR)}',"
