r"""GameClip AI - detection experiment (proof of concept).

One decode pass -> per-frame signals -> peaks + game-profile events -> candidates, contact sheets, plot.

Usage:
  python scripts/detect.py input\session1.mp4
  python scripts/detect.py input\ --profile soulslike --hwaccel qsv

Outputs go to analysis/<recording>/ . The original recording is never modified.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import median_filter, uniform_filter1d, maximum_filter1d
from scipy.signal import find_peaks

from gc_common import (ANALYSIS_DIR, PROFILES_DIR, AudioThread, fmt_ts, iter_frames,
                       list_videos, load_config, load_ground_truth, probe)

MOTION_W, MOTION_H = 160, 90


# =========================================================================== profiles

class Marker:
    """A fixed on-screen element (banner, boss bar...) found by template matching."""

    def __init__(self, spec: dict, profile_dir: Path, analysis_w: int, analysis_h: int):
        self.id = spec["id"]
        self.event = spec.get("event", self.id)
        self.threshold = float(spec.get("threshold", 0.7))
        self.min_frames = 1
        self.min_duration_s = float(spec.get("min_duration_s", 0.6))
        # bridge short dropouts (score flickering around the threshold, a hit-flash over the banner)
        self.gap_fill_s = float(spec.get("gap_fill_s", 1.0))
        rx, ry, rw, rh = spec["roi"]  # fractions of the frame
        self.roi = (int(rx * analysis_w), int(ry * analysis_h),
                    max(1, int(rw * analysis_w)), max(1, int(rh * analysis_h)))
        tpl = cv2.imread(str(profile_dir / spec["template"]), cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            raise SystemExit(f"Template not found: {profile_dir / spec['template']}")
        mask = None
        if spec.get("mask"):
            mask = cv2.imread(str(profile_dir / spec["mask"]), cv2.IMREAD_GRAYSCALE)
        scale = analysis_w / float(spec.get("ref_width", analysis_w))
        if abs(scale - 1) > 1e-3:
            size = (max(1, round(tpl.shape[1] * scale)), max(1, round(tpl.shape[0] * scale)))
            tpl = cv2.resize(tpl, size, interpolation=cv2.INTER_AREA)
            if mask is not None:
                mask = cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST)
        self.tpl, self.mask = tpl, mask

    def score(self, gray_full: np.ndarray) -> float:
        x, y, w, h = self.roi
        region = gray_full[y:y + h, x:x + w]
        th, tw = self.tpl.shape
        if region.shape[0] < th or region.shape[1] < tw:
            return 0.0
        if self.mask is not None:
            res = cv2.matchTemplate(region, self.tpl, cv2.TM_CCOEFF_NORMED, mask=self.mask)
        else:
            res = cv2.matchTemplate(region, self.tpl, cv2.TM_CCOEFF_NORMED)
        res = np.clip(np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0), -1, 1)
        return float(res.max())


def load_profile(name: str | None, cfg: dict) -> tuple[dict, list[Marker]]:
    """No profile -> universal analysis only. A profile may raise the analysis resolution
    (e.g. 640x360) so small HUD elements are readable; motion still runs at 160x90."""
    if not name:
        return {"name": "generic", "markers": []}, []
    pdir = PROFILES_DIR / name
    if not (pdir / "profile.json").exists():
        print(f"  ! profile '{name}' not found - falling back to universal analysis")
        return {"name": "generic", "markers": []}, []
    spec = json.loads((pdir / "profile.json").read_text(encoding="utf-8"))
    a = cfg["analysis"]
    if "analysis_width" in spec:
        a["width"], a["height"] = int(spec["analysis_width"]), int(spec["analysis_height"])
    markers = [Marker(m, pdir, a["width"], a["height"]) for m in spec.get("markers", [])]
    return spec, markers


# =========================================================================== video signals

def video_features(info, cfg, hwaccel, markers, log_path, timeline_every: float = 0.0):
    a = cfg["analysis"]
    win = cv2.createHanningWindow((MOTION_W, MOTION_H), cv2.CV_32F)
    rows = {k: [] for k in ("luma", "raw", "residual", "camera", "hist_corr")}
    mscores = {m.id: [] for m in markers}
    tl_step = max(1, int(round(timeline_every * a["fps"]))) if timeline_every else 0
    timeline_frames = []
    prev_g, prev_h = None, None
    for rgb in iter_frames(info, a["width"], a["height"], a["fps"], hwaccel=hwaccel, log_path=log_path):
        if tl_step and len(rows["luma"]) % tl_step == 0:  # whole-session timeline from the same decode pass
            timeline_frames.append((len(rows["luma"]) / a["fps"], cv2.resize(rgb, (320, 180))))
        small = cv2.resize(rgb, (MOTION_W, MOTION_H), interpolation=cv2.INTER_AREA)
        g = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY).astype(np.float32)
        hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        if prev_g is None:
            raw = residual = cam = 0.0
            corr = 1.0
        else:
            raw = float(np.mean(np.abs(g - prev_g)))
            # copies: OpenCV 5's phaseCorrelate applies the window to its inputs IN PLACE
            (dx, dy), _resp = cv2.phaseCorrelate(prev_g.copy(), g.copy(), win)
            M = np.float32([[1, 0, dx], [0, 1, dy]])
            warped = cv2.warpAffine(prev_g, M, (MOTION_W, MOTION_H), borderMode=cv2.BORDER_REPLICATE)
            m = min(int(math.ceil(max(abs(dx), abs(dy)))) + 2, MOTION_H // 3)
            residual = float(np.mean(np.abs(g[m:-m, m:-m] - warped[m:-m, m:-m])))
            residual = min(residual, raw)
            cam = float(math.hypot(dx, dy))
            corr = float(cv2.compareHist(prev_h, hist, cv2.HISTCMP_CORREL))
            if corr < cfg["motion"]["cut_hist_corr"]:
                residual = cam = 0.0  # a hard cut is an edit/transition, not on-screen action
        rows["luma"].append(float(g.mean()))
        rows["raw"].append(raw)
        rows["residual"].append(residual)
        rows["camera"].append(cam)
        rows["hist_corr"].append(corr)
        if markers:
            gray_full = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            for mk in markers:
                mscores[mk.id].append(mk.score(gray_full))
        prev_g, prev_h = g, hist
    out = {k: np.asarray(v, np.float32) for k, v in rows.items()}
    # a 1-frame spike (0.2 s) is a cut the histogram missed, not action: real action lasts longer
    for k in ("residual", "camera"):
        if len(out[k]) >= 3:
            out[k] = median_filter(out[k], size=3, mode="nearest").astype(np.float32)
    out["markers"] = {k: np.asarray(v, np.float32) for k, v in mscores.items()}
    out["timeline_frames"] = timeline_frames
    return out


# =========================================================================== audio signals

def audio_features(samples: np.ndarray, rate: int, fps: float, n: int, cfg: dict) -> dict:
    hop = int(round(rate / fps))
    db = np.full(n, -90.0, np.float32)
    onset = np.zeros(n, np.float32)
    if samples.size >= hop:
        m = min(n, samples.size // hop)
        blocks = samples[: m * hop].reshape(m, hop)
        db[:m] = 20 * np.log10(np.sqrt(np.mean(blocks ** 2, axis=1)) + 1e-6)
        # spectral flux (onset strength): good for hits, gunshots, explosions; ignores steady music
        nfft, step = 1024, 256
        if samples.size > nfft:
            frames = np.lib.stride_tricks.sliding_window_view(samples, nfft)[::step]
            w = np.hanning(nfft).astype(np.float32)
            flux = np.zeros(len(frames), np.float32)
            flat = np.ones(len(frames), np.float32)
            b0, b1 = int(100 * nfft / rate), int(5000 * nfft / rate)
            prev = None
            for i in range(0, len(frames), 8192):
                lin = np.abs(np.fft.rfft(frames[i:i + 8192] * w, axis=1)).astype(np.float32)
                mag = np.log1p(10 * lin)
                if prev is not None:
                    mag_full = np.vstack([prev[None], mag])
                else:
                    mag_full = np.vstack([mag[:1], mag])
                flux[i:i + len(mag)] = np.maximum(0, np.diff(mag_full, axis=0)).sum(axis=1)
                band = lin[:, b0:b1] + 1e-7
                flat[i:i + len(mag)] = np.exp(np.log(band).mean(axis=1)) / band.mean(axis=1)
                prev = mag[-1]
            # each analysis bin = max flux within it
            bin_idx = (np.arange(len(flux)) * step + nfft // 2) // hop
            keep = bin_idx < n
            np.maximum.at(onset, bin_idx[keep], flux[keep])
            music = music_likelihood(flux, flat, rate / step, n, fps)
    else:
        music = np.zeros(n, np.float32)
    bg_size = max(3, int(cfg["audio"]["background_window_s"] * fps) | 1)
    bg = median_filter(db, size=bg_size, mode="nearest")
    return {"audio_db": db, "audio_rise": (db - bg).astype(np.float32), "onset": onset,
            "music": music if samples.size > 1024 else np.zeros(n, np.float32)}


def music_likelihood(flux: np.ndarray, flat: np.ndarray, frame_rate: float, n: int, fps: float) -> np.ndarray:
    """0..1 per analysis frame: how much the audio looks like MUSIC (e.g. car radio) rather than engines/city.

    Music = tonal (low spectral flatness) AND rhythmic (onset envelope repeats at a 70-180 BPM period).
    Engine noise is noisy (flat), speech is tonal but not periodic. A heuristic flag for review, not proof.
    """
    win, hop = int(6 * frame_rate), int(1 * frame_rate)
    lo, hi = int(frame_rate * 60 / 180), int(frame_rate * 60 / 70)
    per_sec = []
    env = flux - uniform_filter1d(flux, int(frame_rate))  # remove slow loudness changes
    for s in range(0, max(1, len(flux) - win), hop):
        e = env[s:s + win] - env[s:s + win].mean()
        denom = float((e * e).sum()) + 1e-9
        ac = np.array([(e[:-lag] * e[lag:]).sum() / denom for lag in range(lo, hi)])
        beat = float(max(0.0, ac.max())) if ac.size else 0.0
        tonal = float(1.0 - np.median(flat[s:s + win]))
        per_sec.append(beat * tonal)
    per_sec = np.asarray(per_sec, np.float32)
    t_sec = (np.arange(n) / fps).astype(int) - 3  # window centre
    return per_sec[np.clip(t_sec, 0, max(0, len(per_sec) - 1))] if per_sec.size else np.zeros(n, np.float32)


# =========================================================================== helpers

def robust_z(x: np.ndarray) -> np.ndarray:
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826
    return (x - med) / (mad + 1e-6)


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index runs where mask is True."""
    if not mask.any():
        return []
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def fill_gaps(mask: np.ndarray, max_gap: int) -> np.ndarray:
    if max_gap <= 0:
        return mask
    out = mask.copy()
    r = runs(mask)
    for (s0, e0), (s1, _e1) in zip(r, r[1:]):
        if s1 - e0 <= max_gap:
            out[e0:s1] = True
    return out


def presence(scores: np.ndarray, mk: Marker, fps: float) -> list[tuple[int, int]]:
    mask = fill_gaps(scores >= mk.threshold, int(mk.gap_fill_s * fps))
    min_len = max(1, int(mk.min_duration_s * fps))
    return [(s, e) for s, e in runs(mask) if e - s >= min_len]


# =========================================================================== candidates

def profile_events(sig, markers, profile, cfg, fps, duration):
    """Turn marker presence into events, using the profile's data-driven 'encounters' rules.

    An encounter is a span while some marker is on screen (boss bar, round health bars...).
    'inside' maps marker events seen during it to new kinds (death -> boss_death);
    'ends_with' confirms the outcome only if that marker appears shortly after the span ends.
    Nothing here knows about any specific game or genre.
    """
    ctx = {**cfg["context"], **profile.get("context", {})}
    by_event: dict[str, list[tuple[float, float, str]]] = {}
    for mk in markers:
        for s, e in presence(sig["markers"][mk.id], mk, fps):
            by_event.setdefault(mk.event, []).append((s / fps, e / fps, mk.id))

    events, used = [], set()  # used: (event_name, index) consumed by an encounter rule
    container_events = set()
    for rule in profile.get("encounters", []):
        cont = rule["while"]
        container_events.add(cont)
        spans = by_event.get(cont, [])
        if rule.get("gap_fill_s"):
            merged = []
            for s, e, mid in sorted(spans):
                if merged and s - merged[-1][1] <= rule["gap_fill_s"]:
                    merged[-1] = (merged[-1][0], e, mid)
                else:
                    merged.append((s, e, mid))
            spans = merged
        for es, ee, _ in spans:
            enc_name = rule.get("name", cont)
            had_inside = False
            for ev_name, kind in rule.get("inside", {}).items():
                for i, (ds, _de, mid) in enumerate(by_event.get(ev_name, [])):
                    if es <= ds <= ee + 3:
                        used.add((ev_name, i)); had_inside = True
                        events.append(dict(kind=kind, t=ds, enc=(es, ee), marker=mid, confidence="medium",
                                           reason=f"'{mid}' during {enc_name} ({fmt_ts(es)}-{fmt_ts(ee)})"))
            confirmed = False
            for ev_name, spec in rule.get("ends_with", {}).items():
                hits = [(i, v) for i, v in enumerate(by_event.get(ev_name, []))
                        if ee - 2 <= v[0] <= ee + spec.get("within_s", 15)]
                if hits:
                    i, (vs, _ve, mid) = hits[0]
                    used.add((ev_name, i)); confirmed = True
                    events.append(dict(kind=spec["kind"], t=vs, enc=(es, ee), marker=mid, confidence="high",
                                       reason=f"{enc_name} ended {fmt_ts(ee)}, then '{mid}' at {fmt_ts(vs)}"))
                    break
            if not confirmed and not had_inside and rule.get("unconfirmed_end_kind"):
                events.append(dict(kind=rule["unconfirmed_end_kind"], t=ee, enc=(es, ee), marker=cont,
                                   confidence="low",
                                   reason=f"{enc_name} marker disappeared without a confirming marker - unconfirmed"))

    for ev_name, items in by_event.items():
        if ev_name in container_events:
            continue
        for i, (s, _e, mid) in enumerate(items):
            if (ev_name, i) not in used:
                events.append(dict(kind=ev_name, t=s, marker=mid, confidence="medium",
                                   reason=f"'{mid}' marker detected"))

    cands = []
    for ev in events:
        c = ctx.get(ev["kind"], ctx.get("marker"))
        start, end = ev["t"] - c["pre"], ev["t"] + c["post"]
        if "enc" in ev:
            start = max(start, ev["enc"][0] - 3)  # include the boss intro if the fight is short
        conf = ev.get("confidence", "medium")
        cand = dict(kind=ev["kind"], source="profile", peak_t=ev["t"],
                    start=max(0.0, start), end=min(duration, end),
                    score=10.0 + {"high": 3, "medium": 1, "low": 0}[conf],
                    confidence=conf, verified=False, reason=ev["reason"])
        if "enc" in ev:
            cand["encounter_start"], cand["encounter_end"] = round(ev["enc"][0], 2), round(ev["enc"][1], 2)
        cands.append(cand)
    return cands


def generic_candidates(sig, cfg, fps, duration):
    sc, mo = cfg["score"], cfg["motion"]
    n = len(sig["raw"])
    static = np.zeros(n, bool)
    for s, e in runs(sig["raw"] < mo["static_diff"]):
        if e - s >= mo["static_min_s"] * fps:
            static[s:e] = True
    black = sig["luma"] < mo["black_luma"]
    gameplay = ~(static | black)

    rise = sig["audio_rise"]
    onset_z = np.clip(robust_z(sig["onset"]), 0, 6)
    resid_z = np.clip(robust_z(sig["residual"]), 0, 6)
    cam_z = np.clip(robust_z(sig["camera"]), 0, 4)
    rise_n = np.clip((rise - 3.0) / 3.0, 0, 4)
    score = (sc["w_audio_rise"] * rise_n + sc["w_audio_onset"] * onset_z +
             sc["w_residual"] * resid_z + sc["w_camera"] * cam_z) * gameplay
    score_s = uniform_filter1d(score, max(1, int(sc["smooth_s"] * fps)))

    # absolute floor: something must actually be loud or busy, not just "less boring than the rest"
    win = max(1, int(2 * fps))
    floor_ok = (maximum_filter1d(rise, win) >= cfg["audio"]["min_rise_db"]) | \
               (maximum_filter1d(sig["residual"], win) >= mo["residual_floor"])

    peaks, _ = find_peaks(score_s, height=sc["min_score"], distance=max(1, int(cfg["peaks"]["min_gap_s"] * fps)))
    ctx = cfg["context"]["generic"]
    cands = []
    for p in peaks:
        if not floor_ok[p]:
            continue
        lo, hi = max(0, p - int(fps)), min(n, p + int(fps) + 1)
        ev = {
            "audio_rise_db": round(float(rise[lo:hi].max()), 1),
            "onset_z": round(float(onset_z[lo:hi].max()), 1),
            "residual_motion": round(float(sig["residual"][lo:hi].max()), 1),
            "camera_speed": round(float(sig["camera"][lo:hi].max()), 1),
        }
        parts = []
        if ev["audio_rise_db"] >= cfg["audio"]["min_rise_db"]:
            parts.append(f"audio +{ev['audio_rise_db']} dB over background")
        if ev["onset_z"] >= 3:
            parts.append(f"sharp sound hits (z={ev['onset_z']})")
        if ev["residual_motion"] >= mo["residual_floor"]:
            parts.append(f"on-screen action beyond camera movement ({ev['residual_motion']})")
        t = p / fps
        cands.append(dict(kind="generic", source="signals", peak_t=t,
                          start=max(0.0, t - ctx["pre"]), end=min(duration, t + ctx["post"]),
                          score=round(float(score_s[p]), 3), confidence="unverified", verified=False,
                          reason="; ".join(parts) or "combined signal peak", evidence=ev))
    return cands, {"gameplay": gameplay, "static": static, "black": black, "score": score_s}


def merge_candidates(profile_c, generic_c, cfg):
    gap = cfg["peaks"]["merge_gap_s"]
    kept = list(profile_c)
    for g in sorted(generic_c, key=lambda c: -c["score"]):
        clash = next((k for k in kept if g["start"] < k["end"] + gap and k["start"] < g["end"] + gap), None)
        if clash is None:
            kept.append(g)
        elif clash["source"] == "signals":
            clash["start"], clash["end"] = min(clash["start"], g["start"]), max(clash["end"], g["end"])
        # generic peaks inside a profile event just add support
        elif "support" not in clash:
            clash["support"] = g["reason"]
    kept.sort(key=lambda c: -c["score"])
    return kept[: cfg["peaks"]["max_candidates"]]


def snap_to_cuts(cands, hist_corr, cfg, fps, duration):
    """Move a boundary to a nearby hard cut only if it is close and never eats into the event."""
    cut_idx = np.flatnonzero(hist_corr < cfg["motion"]["cut_hist_corr"])
    cuts = cut_idx / fps
    lim = cfg["context"]["snap_max_s"]
    for c in cands:
        near = cuts[(np.abs(cuts - c["start"]) <= lim) & (cuts <= c["peak_t"] - 2)]
        if near.size:
            c["start"] = float(near[np.argmin(np.abs(near - c["start"]))])
        near = cuts[(np.abs(cuts - c["end"]) <= lim) & (cuts >= c["peak_t"] + 1)]
        if near.size:
            c["end"] = float(near[np.argmin(np.abs(near - c["end"]))])
        c["start"], c["end"] = max(0.0, c["start"]), min(duration, c["end"])


# =========================================================================== outputs

def _font(size):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def contact_sheet(info, cand, rank, cfg, hwaccel, out_path):
    cs = cfg["contact_sheet"]
    n, cols, tw, th = cs["frames"], cs["cols"], cs["tile_w"], cs["tile_h"]
    dur = max(0.5, cand["end"] - cand["start"])
    fps = n / dur
    frames = []
    for f in iter_frames(info, tw, th, fps, hwaccel=hwaccel, start=cand["start"], duration=dur):
        frames.append(f.copy())
        if len(frames) >= n:
            break
    if not frames:
        return None
    rows_n = math.ceil(len(frames) / cols)
    header = 44
    sheet = Image.new("RGB", (cols * tw, header + rows_n * th), (18, 18, 18))
    d = ImageDraw.Draw(sheet)
    f_big, f_small = _font(20), _font(16)
    d.text((10, 4), f"#{rank}  {cand['kind']}  {fmt_ts(cand['start'])} - {fmt_ts(cand['end'])}  "
                    f"peak {fmt_ts(cand['peak_t'])}  score {cand['score']:.2f}", fill=(255, 255, 255), font=f_big)
    d.text((10, 26), cand["reason"][:150], fill=(190, 190, 190), font=f_small)
    times = [cand["start"] + i / fps for i in range(len(frames))]
    peak_i = int(np.argmin([abs(t - cand["peak_t"]) for t in times]))
    for i, (fr, t) in enumerate(zip(frames, times)):
        x, y = (i % cols) * tw, header + (i // cols) * th
        sheet.paste(Image.fromarray(fr), (x, y))
        label = fmt_ts(t)
        d.rectangle([x + 4, y + 4, x + 8 + 9 * len(label), y + 24], fill=(0, 0, 0))
        d.text((x + 6, y + 5), label, fill=(255, 230, 0), font=f_small)
        if i == peak_i:
            d.rectangle([x + 1, y + 1, x + tw - 2, y + th - 2], outline=(255, 40, 40), width=4)
    sheet.save(out_path, quality=cs["jpeg_quality"])
    return out_path


def peak_frame(info, t, hwaccel, out_path, max_w=1920):
    """One sharp frame at the event (source resolution, capped at 1920 wide, HDR tone-mapped)."""
    w = min(max_w, info.width)
    h = int(round(info.height * w / info.width / 2) * 2)
    for f in iter_frames(info, w, h, None, hwaccel=hwaccel, start=max(0.0, t), duration=0.5):
        Image.fromarray(f).save(out_path, quality=90)
        return out_path
    return None


def plot_signals(sig, extra, cands, fps, gt, out_path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t = np.arange(len(sig["raw"])) / fps / 60.0
    fig, ax = plt.subplots(4, 1, figsize=(max(14, len(t) / fps / 20), 9), sharex=True)
    ax[0].plot(t, sig["audio_rise"], lw=0.6); ax[0].set_ylabel("audio rise dB")
    ax[1].plot(t, robust_z(sig["onset"]), lw=0.6, color="tab:orange"); ax[1].set_ylabel("onset z")
    ax[2].plot(t, sig["residual"], lw=0.6, color="tab:green", label="action")
    ax[2].plot(t, sig["camera"], lw=0.6, color="tab:gray", alpha=0.6, label="camera")
    ax[2].legend(loc="upper right"); ax[2].set_ylabel("motion")
    ax[3].plot(t, extra["score"], lw=0.8, color="k"); ax[3].set_ylabel("score")
    for a in ax:
        for s, e in runs(~extra["gameplay"]):
            a.axvspan(s / fps / 60, e / fps / 60, color="0.85", lw=0)
        for g in gt:
            a.axvspan(g["start"] / 60, g["end"] / 60, color="tab:green" if g["interesting"] else "tab:red",
                      alpha=0.15, lw=0)
    for i, c in enumerate(cands, 1):
        ax[3].axvspan(c["start"] / 60, c["end"] / 60, color="tab:purple" if c["source"] == "profile" else "tab:blue",
                      alpha=0.25, lw=0)
        ax[3].text(c["peak_t"] / 60, ax[3].get_ylim()[1] * 0.9, str(i), fontsize=7, ha="center")
    ax[-1].set_xlabel("minutes  (grey = static/black, green/red = your labels, blue/purple = candidates)")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def write_outputs(out_dir, info, sig, extra, cands, fps):
    with open(out_dir / "candidates.json", "w", encoding="utf-8") as f:
        json.dump(cands, f, indent=2)
    keys = ["rank", "kind", "source", "confidence", "verified", "start", "end", "peak_t", "start_ts", "end_ts",
            "score", "reason", "sheet", "peak_frame"]
    with open(out_dir / "candidates.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for c in cands:
            w.writerow(c)
    with open(out_dir / "timeline.csv", "w", encoding="utf-8", newline="") as f:
        cols = ["t", "luma", "raw", "residual", "camera", "hist_corr", "audio_db", "audio_rise", "onset", "music",
                "score", "gameplay"] + [f"m_{k}" for k in sig["markers"]]
        w = csv.writer(f)
        w.writerow(cols)
        for i in range(len(sig["raw"])):
            w.writerow([round(i / fps, 2)] + [round(float(sig[k][i]), 3) for k in
                        ("luma", "raw", "residual", "camera", "hist_corr", "audio_db", "audio_rise", "onset",
                         "music")] +
                       [round(float(extra["score"][i]), 3), int(extra["gameplay"][i])] +
                       [round(float(v[i]), 3) for v in sig["markers"].values()])


# =========================================================================== main

def analyze(path: Path, cfg: dict, profile_name: str | None, hwaccel: str, sheets: bool,
            timeline_every: float = 0.0) -> dict:
    t0 = time.perf_counter()
    info = probe(path)
    if hwaccel == "auto":
        # measured on real PS5 files: GPU wins for 10-bit HDR VP9, CPU wins for 8-bit H.264
        hwaccel = "d3d11va" if (info.is_hdr or "10" in info.pix_fmt) else "none"
    a = cfg["analysis"]
    fps = float(a["fps"])
    out_dir = ANALYSIS_DIR / path.stem
    (out_dir / "sheets").mkdir(parents=True, exist_ok=True)
    for old in (out_dir / "sheets").glob("cand_*.jpg"):  # only our own previous outputs, never input/
        old.unlink()
    log_path = out_dir / "ffmpeg.log"
    log_path.write_text("", encoding="utf-8")
    profile, markers = load_profile(profile_name, cfg)
    print(f"[{path.name}] {info.summary()['video']}  hdr={info.is_hdr}  {fmt_ts(info.duration)}  "
          f"profile={profile.get('name')}  decode={hwaccel}")

    at = AudioThread(info, a["audio_rate"]); at.start()
    sig = video_features(info, cfg, hwaccel, markers, log_path, timeline_every)
    t_video = time.perf_counter() - t0
    tl = sig.pop("timeline_frames", [])
    if tl:
        from overview import write_pages
        pages = write_pages(tl, out_dir / "timeline")
        print(f"  timeline: {pages} pages, one frame every {timeline_every:g}s -> {out_dir / 'timeline'}")
    at.join()
    if at.error:
        raise at.error
    n = len(sig["raw"])
    if n == 0:
        raise SystemExit(f"No frames decoded - check {log_path}")
    sig.update(audio_features(at.samples, a["audio_rate"], fps, n, cfg))
    duration = min(info.duration, n / fps) if info.duration else n / fps

    p_c = profile_events(sig, markers, profile, cfg, fps, duration) if markers else []
    g_c, extra = generic_candidates(sig, cfg, fps, duration)
    cands = merge_candidates(p_c, g_c, cfg)
    snap_to_cuts(cands, sig["hist_corr"], cfg, fps, duration)
    for i, c in enumerate(cands, 1):
        c["rank"] = i
        lo, hi = int(c["start"] * fps), max(int(c["start"] * fps) + 1, int(c["end"] * fps))
        c["music_max"] = round(float(sig["music"][lo:hi].max()), 2) if hi <= len(sig["music"]) else 0.0
        c["start"], c["end"], c["peak_t"] = round(c["start"], 2), round(c["end"], 2), round(c["peak_t"], 2)
        c["start_ts"], c["end_ts"] = fmt_ts(c["start"]), fmt_ts(c["end"])
        if sheets:
            p = contact_sheet(info, c, i, cfg, hwaccel, out_dir / "sheets" / f"cand_{i:02d}.jpg")
            c["sheet"] = str(p.relative_to(out_dir)) if p else ""
            p = peak_frame(info, c["peak_t"], hwaccel, out_dir / "sheets" / f"cand_{i:02d}_peak.jpg")
            c["peak_frame"] = str(p.relative_to(out_dir)) if p else ""

    gt = load_ground_truth(path.stem)
    write_outputs(out_dir, info, sig, extra, cands, fps)
    plot_signals(sig, extra, cands, fps, gt, out_dir / "signals.png", f"{path.name}  ({profile.get('name')})")
    total = time.perf_counter() - t0
    meta = {
        "recording": path.name, "media": info.summary(), "profile": profile.get("name"),
        "profile_calibrated": profile.get("calibrated", None), "decode": hwaccel,
        "frames": n, "analysis_fps": fps,
        "timing_s": {"decode_and_features": round(t_video, 1), "total": round(total, 1)},
        "x_realtime": round(duration / total, 2) if total else None,
        "candidates": len(cands),
        "by_kind": {k: sum(1 for c in cands if c["kind"] == k) for k in sorted({c["kind"] for c in cands})},
        "non_gameplay_pct": round(100 * float((~extra["gameplay"]).mean()), 1),
        "music_likely_pct": round(100 * float((sig["music"] > 0.25).mean()), 1),
        "config": cfg,
    }
    (out_dir / "analysis.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"  -> {len(cands)} candidates {meta['by_kind']}  non-gameplay {meta['non_gameplay_pct']}%  "
          f"{total:.1f}s ({meta['x_realtime']}x realtime)  -> {out_dir}")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="video file or folder")
    ap.add_argument("--profile", help="folder name under profiles/ (e.g. soulslike)")
    ap.add_argument("--hwaccel", default="auto", choices=["auto", "none", "qsv", "d3d11va"])
    ap.add_argument("--config", type=Path)
    ap.add_argument("--no-sheets", action="store_true")
    ap.add_argument("--timeline-every", type=float, default=10.0,
                    help="also save whole-session timeline pages from the same decode pass (0 = off)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    for v in list_videos(Path(args.target)):
        analyze(v, cfg, args.profile, args.hwaccel, not args.no_sheets, args.timeline_every)


if __name__ == "__main__":
    main()
