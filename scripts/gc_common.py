"""Shared helpers for GameClip AI: paths, config, ffprobe, ffmpeg decode pipes, HDR tone mapping.

Original recordings are only ever READ. Nothing in this module writes next to them.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = ROOT / "input"
ANALYSIS_DIR = ROOT / "analysis"
PROFILES_DIR = ROOT / "profiles"
DATASET_DIR = ROOT / "dataset"
CONFIG_PATH = ROOT / "config" / "default.json"

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov"}
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


def ffbin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    fallback = Path(r"D:\Tools\ffmpeg\bin") / f"{name}.exe"
    if fallback.exists():
        return str(fallback)
    raise SystemExit(f"{name} not found on PATH or in D:\\Tools\\ffmpeg\\bin")


def load_config(path: Path | None = None) -> dict:
    with open(path or CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def list_videos(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(p for p in target.iterdir() if p.suffix.lower() in VIDEO_EXTS)
    return [target]


def fmt_ts(sec: float) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h >= 1 else f"{int(m):02d}:{s:04.1f}"


# --------------------------------------------------------------------------- probe

@dataclass
class MediaInfo:
    path: Path
    container: str
    duration: float
    size_bytes: int
    vcodec: str
    width: int
    height: int
    fps: float
    pix_fmt: str
    color_transfer: str
    color_primaries: str
    color_space: str
    acodec: str | None
    audio_channels: int
    audio_rate: int
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in HDR_TRANSFERS

    def summary(self) -> dict:
        return {
            "file": self.path.name,
            "container": self.container,
            "duration_s": round(self.duration, 2),
            "size_gb": round(self.size_bytes / 1e9, 2),
            "video": f"{self.vcodec} {self.width}x{self.height} @ {self.fps:.2f} fps, {self.pix_fmt}",
            "hdr": self.is_hdr,
            "color": f"transfer={self.color_transfer} primaries={self.color_primaries} space={self.color_space}",
            "audio": f"{self.acodec} {self.audio_channels}ch {self.audio_rate} Hz" if self.acodec else "none",
        }


def _rate(s: str | None) -> float:
    if not s or s in ("0/0", "N/A"):
        return 0.0
    if "/" in s:
        n, d = s.split("/")
        return float(n) / float(d) if float(d) else 0.0
    return float(s)


def probe(path: Path) -> MediaInfo:
    cmd = [ffbin("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise SystemExit(f"No video stream in {path}")
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or v.get("duration") or 0)
    return MediaInfo(
        path=path,
        container=fmt.get("format_name", "?"),
        duration=duration,
        size_bytes=int(fmt.get("size") or path.stat().st_size),
        vcodec=v.get("codec_name", "?"),
        width=int(v["width"]),
        height=int(v["height"]),
        fps=_rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate")),
        pix_fmt=v.get("pix_fmt", "?"),
        color_transfer=v.get("color_transfer", "unknown"),
        color_primaries=v.get("color_primaries", "unknown"),
        color_space=v.get("color_space", "unknown"),
        acodec=a.get("codec_name") if a else None,
        audio_channels=int(a.get("channels", 0)) if a else 0,
        audio_rate=int(a.get("sample_rate", 0)) if a else 0,
        raw=data,
    )


# --------------------------------------------------------------------------- filters

def tonemap_chain(info: MediaInfo) -> str:
    """HDR (PQ/HLG, BT.2020) -> SDR BT.709. Applied AFTER downscaling so it stays cheap."""
    if not info.is_hdr:
        return ""
    tin = info.color_transfer
    return (
        f"zscale=tin={tin}:pin=bt2020:min=bt2020nc:rin=tv:t=linear:npl=100,"
        "format=gbrpf32le,zscale=p=bt709,"
        "tonemap=tonemap=hable:desat=0,"
        "zscale=t=bt709:m=bt709:r=tv"
    )


def video_filter(info: MediaInfo, width: int, height: int, fps: float | None, hwaccel: str = "none") -> str:
    parts = []
    if hwaccel in ("qsv", "d3d11va"):
        # Frames stay in GPU memory: drop to the analysis frame rate THERE, then copy only those
        # frames back (10-bit sources -> p010). Measured 1.7x faster than CPU on PS5 HDR VP9.
        if fps:
            parts.append(f"fps={fps}")
        parts.append("hwdownload,format=" + ("p010le" if "10" in info.pix_fmt else "nv12"))
    elif fps:
        parts.append(f"fps={fps}")
    parts.append(f"scale={width}:{height}:flags=area")
    tm = tonemap_chain(info)
    if tm:
        parts.append(tm)
    parts.append("format=rgb24")
    return ",".join(parts)


def hwaccel_args(mode: str) -> list[str]:
    if mode in (None, "", "none", "cpu"):
        return []
    if mode == "qsv":
        return ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
    if mode == "d3d11va":
        return ["-hwaccel", "d3d11va", "-hwaccel_output_format", "d3d11"]
    return ["-hwaccel", mode]


# --------------------------------------------------------------------------- decode

def iter_frames(info: MediaInfo, width: int, height: int, fps: float | None,
                hwaccel: str = "none", start: float | None = None,
                duration: float | None = None, log_path: Path | None = None) -> Iterator[np.ndarray]:
    """Yield RGB uint8 frames (H, W, 3) from a single ffmpeg pipe."""
    cmd = [ffbin("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error"]
    cmd += hwaccel_args(hwaccel)
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(info.path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-an", "-vf", video_filter(info, width, height, fps, hwaccel), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    frame_bytes = width * height * 3
    log = open(log_path, "ab") if log_path else subprocess.DEVNULL
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, bufsize=frame_bytes * 8)
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield np.frombuffer(buf, np.uint8).reshape(height, width, 3)
    finally:
        proc.stdout.close()
        proc.wait()
        if log_path:
            log.close()
    if proc.returncode not in (0, None):
        raise RuntimeError(f"ffmpeg decode failed (exit {proc.returncode}); see {log_path}")


def decode_audio(info: MediaInfo, rate: int) -> np.ndarray:
    """Mono float32 game audio. Empty array if the recording has no audio."""
    if not info.acodec:
        return np.zeros(0, np.float32)
    cmd = [ffbin("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(info.path),
           "-vn", "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, np.float32).copy()


def parse_time(s: str) -> float:
    """Accept '95', '95.5', '1:35', '0:01:35'."""
    s = s.strip()
    if ":" not in s:
        return float(s)
    total = 0.0
    for part in s.split(":"):
        total = total * 60 + float(part)
    return total


def load_ground_truth(recording: str | None = None) -> list[dict]:
    """dataset/ground_truth.csv: recording,start,end,event,interesting,notes (times in s or mm:ss)."""
    import csv
    path = DATASET_DIR / "ground_truth.csv"
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if not r.get("recording") or r["recording"].startswith("#"):
                continue
            if recording and r["recording"].strip() != recording:
                continue
            rows.append({
                "recording": r["recording"].strip(),
                "start": parse_time(r["start"]),
                "end": parse_time(r["end"]),
                "event": (r.get("event") or "").strip(),
                "interesting": (r.get("interesting") or "").strip().lower() in ("true", "yes", "1", "y"),
                "notes": (r.get("notes") or "").strip(),
            })
    return rows


class AudioThread(threading.Thread):
    """Decode audio in parallel with the video pass."""

    def __init__(self, info: MediaInfo, rate: int):
        super().__init__(daemon=True)
        self.info, self.rate = info, rate
        self.samples: np.ndarray | None = None
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            self.samples = decode_audio(self.info, self.rate)
        except Exception as e:  # surfaced by caller
            self.error = e
