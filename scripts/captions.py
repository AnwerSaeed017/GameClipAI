"""Caption graphics for Shorts and long-form: drawn with PIL as transparent PNGs, animated by FFmpeg.

Text spec (in an EDL "texts" list):
  {"lines": ["LORETTA,", "KNIGHT OF THE *HALIGTREE*"], "style": "caption", "start": 2.9, "end": 10.8,
   "y": 1320, "size": 78}
  *word* -> highlight colour (yellow). Styles:
    hook    - huge Impact, white + yellow highlights, heavy outline + shadow (first 1-3 s)
    caption - bold Impact, white + yellow highlights, outline + shadow
    label   - small black-on-yellow rounded pill (game name, chapter tag)
    cta     - white Impact on a red rounded pill (call to action)
Animation: slide up 28 px + fade in over 0.18 s, fade out over 0.2 s.
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = Path("C:/Windows/Fonts")
YELLOW, WHITE, BLACK, RED = (255, 212, 0), (255, 255, 255), (0, 0, 0), (229, 57, 53)

STYLES = {
    "hook":    dict(font="impact.ttf", fill=WHITE, hi=YELLOW, stroke=0.085, shadow=True, pill=None),
    "caption": dict(font="impact.ttf", fill=WHITE, hi=YELLOW, stroke=0.075, shadow=True, pill=None),
    "label":   dict(font="segoeuib.ttf", fill=BLACK, hi=RED, stroke=0, shadow=True, pill=YELLOW),
    "cta":     dict(font="impact.ttf", fill=WHITE, hi=YELLOW, stroke=0, shadow=True, pill=RED),
}


def _tokens(line: str) -> list[tuple[str, bool]]:
    """'KNIGHT OF THE *HALIGTREE*' -> [('KNIGHT OF THE ', False), ('HALIGTREE', True)]"""
    out = []
    for part in re.split(r"(\*[^*]+\*)", line):
        if part:
            out.append((part[1:-1], True) if part.startswith("*") and part.endswith("*") else (part, False))
    return out


PROJECT_FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"


def _rgb(c) -> tuple[int, int, int]:
    if isinstance(c, str):
        c = c.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return tuple(c)


def render_caption(spec: dict, path: Path, max_w: int) -> tuple[int, int]:
    st = dict(STYLES[spec.get("style", "caption")])
    # optional per-caption overrides, e.g. to match the karaoke subtitles:
    #   "font": "Montserrat-Black.ttf", "hi": "#FF4FA0", "pill": "#FF4FA0", "outline": "#19052D"
    for k in ("hi", "pill"):
        if spec.get(k):
            st[k] = _rgb(spec[k])
    if spec.get("font"):
        st["font"] = spec["font"]
    outline = _rgb(spec.get("outline", BLACK))
    font_path = PROJECT_FONTS / st["font"]
    if not font_path.exists():
        font_path = FONT_DIR / st["font"]
    size = int(spec.get("size", 72))
    lines = spec["lines"]

    def font_for(s):
        return ImageFont.truetype(str(font_path), s)

    # shrink until the widest line fits
    while True:
        font = font_for(size)
        sw = max(2, int(size * st["stroke"])) if st["stroke"] else 0
        widths = [sum(font.getlength(t) for t, _ in _tokens(ln)) + 2 * sw for ln in lines]
        pad_x = int(size * 0.45) if st["pill"] else 0
        if max(widths) + 2 * pad_x <= max_w or size <= 24:
            break
        size -= 4
    asc, desc = font.getmetrics()
    line_h = int((asc + desc) * 1.02)
    pad_y = int(size * 0.22) if st["pill"] else 0
    margin = int(size * 0.35)  # room for shadow
    w = int(max(widths)) + 2 * pad_x + 2 * margin
    h = line_h * len(lines) + 2 * pad_y + 2 * margin
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    if st["pill"]:
        pill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(pill).rounded_rectangle([margin, margin, w - margin, h - margin],
                                               radius=int(size * 0.35), fill=st["pill"] + (255,))
        if st["shadow"]:
            sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(sh).rounded_rectangle([margin, margin + 6, w - margin, h - margin + 6],
                                                 radius=int(size * 0.35), fill=(0, 0, 0, 150))
            img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(8)))
        img.alpha_composite(pill)

    text = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(text)
    for i, ln in enumerate(lines):
        lw = widths[i]
        x = (w - lw) / 2 + sw
        y = margin + pad_y + i * line_h
        for tok, hi in _tokens(ln):
            d.text((x, y), tok, font=font, fill=(st["hi"] if hi else st["fill"]) + (255,),
                   stroke_width=sw, stroke_fill=outline + (255,))
            x += font.getlength(tok)
    if st["shadow"] and not st["pill"]:
        alpha = text.split()[3]
        shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        shadow.putalpha(alpha.point(lambda v: int(v * 0.65)))
        shadow = shadow.transform((w, h), Image.AFFINE, (1, 0, -4, 0, 1, -8)).filter(ImageFilter.GaussianBlur(6))
        img.alpha_composite(shadow)
    img.alpha_composite(text)
    img.save(path)
    return w, h


def overlay_graph(texts: list[dict], work: Path, key: str, base: str, first_input: int,
                  canvas_w: int, safe_margin: int) -> tuple[list[str], str, str]:
    """Returns (extra ffmpeg input args, filter snippet, output label) that overlay every caption on `base`."""
    args, graph, cur = [], "", base
    for k, t in enumerate(texts):
        png = work / f"{key}_cap{k}.png"
        w, h = render_caption(t, png, canvas_w - 2 * safe_margin)
        a, b = float(t["start"]), float(t["end"])
        idx = first_input + k
        args += ["-loop", "1", "-i", str(png)]
        y0 = int(t["y"]) - int(t.get("size", 72) * 0.35)  # PNG margin compensation
        instant = a < 0.05  # a hook on the very first frame must be fully visible at once
        fade_in = "" if instant else f"fade=t=in:st={a}:d=0.18:alpha=1,"
        y_expr = f"{y0}" if instant else f"'{y0}+28*max(0\\,1-(t-{a})/0.18)'"
        graph += (f"[{idx}:v]format=rgba,{fade_in}"
                  f"fade=t=out:st={max(a, b - 0.2):.3f}:d=0.2:alpha=1[{key}c{k}];"
                  f"[{cur}][{key}c{k}]overlay=x=(W-w)/2:y={y_expr}"
                  f":enable='between(t,{a},{b})':eval=frame:shortest=1[{key}o{k}];")
        cur = f"{key}o{k}"
    return args, graph, cur
