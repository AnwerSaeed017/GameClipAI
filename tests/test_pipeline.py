"""Tests for the detection prototype. Run:  .venv\\Scripts\\python.exe -m unittest discover -s tests -v

Synthetic videos verify that the SOFTWARE behaves correctly (timestamps, camera compensation,
static/black filtering, zero-candidate sessions, profile rules, HDR path, scoring maths).
They do NOT validate gameplay detection - only your labelled PS5 recordings can do that.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import detect  # noqa: E402
import evaluate  # noqa: E402
import make_fixtures as fx  # noqa: E402
from gc_common import PROFILES_DIR, load_config, parse_time, probe  # noqa: E402

FIX = fx.FIX
TEST_PROFILE = "_synthetic_test"
PY = sys.executable


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def ensure_fixtures():
    need = ["synthetic_events", "synthetic_boring", "synthetic_markers", "synthetic_hdr", "synthetic_noaudio"]
    if not all((FIX / f"{n}.mp4").exists() for n in need):
        fx.main()


class TestDetector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_fixtures()
        cls.cfg = load_config()
        cls.hash_before = sha(FIX / "synthetic_events.mp4")
        cls.meta = detect.analyze(FIX / "synthetic_events.mp4", load_config(), None, "none", True)
        cls.cands = json.loads((ROOT / "analysis/synthetic_events/candidates.json").read_text())

    def test_known_events_found(self):
        for s, e in fx.EVENTS_TRUTH["positives"]:
            self.assertTrue(any(s - 2.5 <= c["peak_t"] <= e + 2.5 for c in self.cands),
                            f"event {s}-{e}s not found; peaks={[c['peak_t'] for c in self.cands]}")

    def test_no_false_candidates_in_quiet_zones(self):
        # static menu, fast camera pan only, sustained loud music, black loading screen
        for s, e in fx.EVENTS_TRUTH["no_candidate_zones"]:
            bad = [c["peak_t"] for c in self.cands if s <= c["peak_t"] <= e]
            self.assertFalse(bad, f"unexpected candidate peak(s) {bad} inside {s}-{e}s")

    def test_candidates_are_evidence_based_and_unverified(self):
        for c in self.cands:
            self.assertFalse(c["verified"])
            self.assertEqual(c["confidence"], "unverified")
            self.assertTrue(c["reason"])
            self.assertLessEqual(c["start"], c["peak_t"]); self.assertLessEqual(c["peak_t"], c["end"])
            self.assertLessEqual(c["end"], 120.0)

    def test_no_duplicate_candidates(self):
        peaks = sorted(c["peak_t"] for c in self.cands)
        self.assertTrue(all(b - a >= self.cfg["peaks"]["min_gap_s"] for a, b in zip(peaks, peaks[1:])))

    def test_non_gameplay_detected(self):
        # 10 s static + 10 s black out of 120 s
        self.assertAlmostEqual(self.meta["non_gameplay_pct"], 16.7, delta=2.0)

    def test_contact_sheet_and_peak_frame(self):
        c = self.cands[0]
        cs = self.cfg["contact_sheet"]
        with Image.open(ROOT / "analysis/synthetic_events" / c["sheet"]) as sheet:
            self.assertEqual(sheet.width, cs["cols"] * cs["tile_w"])
        with Image.open(ROOT / "analysis/synthetic_events" / c["peak_frame"]) as peak:
            self.assertEqual(peak.size, (640, 360))

    def test_timestamps_match_source(self):
        # frame at a known time must equal the source frame generated for that time
        info = probe(FIX / "synthetic_events.mp4")
        t = 15.0
        f = next(iter(detect.iter_frames(info, 640, 360, None, start=t, duration=0.2))).astype(np.float32)
        expect = fx.events_frame(t).astype(np.float32)
        wrong = fx.events_frame(t + 1.0).astype(np.float32)
        self.assertLess(np.abs(f - expect).mean(), np.abs(f - wrong).mean() / 3)

    def test_original_untouched(self):
        self.assertEqual(self.hash_before, sha(FIX / "synthetic_events.mp4"))


class TestEdgeCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_fixtures()

    def test_boring_session_returns_zero(self):
        m = detect.analyze(FIX / "synthetic_boring.mp4", load_config(), None, "none", False)
        self.assertEqual(m["candidates"], 0)

    def test_no_audio_stream(self):
        m = detect.analyze(FIX / "synthetic_noaudio.mp4", load_config(), None, "none", False)
        self.assertEqual(m["media"]["audio"], "none")

    def test_hdr_is_detected_and_tone_mapped(self):
        info = probe(FIX / "synthetic_hdr.mp4")
        self.assertTrue(info.is_hdr)
        out = ROOT / "analysis" / "hdr_peak_test.jpg"
        detect.peak_frame(info, 5.0, "none", out)
        arr = np.asarray(Image.open(out)).astype(np.float32)
        self.assertTrue(10 < arr.mean() < 245, f"tone-mapped frame looks broken (mean {arr.mean():.1f})")
        out.unlink()

    def test_missing_profile_falls_back(self):
        spec, markers = detect.load_profile("no_such_game", load_config())
        self.assertEqual(spec["name"], "generic"); self.assertEqual(markers, [])

    def test_parse_time(self):
        self.assertEqual(parse_time("95"), 95); self.assertEqual(parse_time("1:35"), 95)
        self.assertEqual(parse_time("0:01:35.5"), 95.5)


class TestProfileEvents(unittest.TestCase):
    """Builds a profile from the synthetic footage with make_template.py, like you will with real footage."""

    @classmethod
    def setUpClass(cls):
        ensure_fixtures()
        shutil.rmtree(PROFILES_DIR / TEST_PROFILE, ignore_errors=True)
        vid = str(FIX / "synthetic_markers.mp4")
        mk = [PY, str(ROOT / "scripts/make_template.py"), "crop", vid, "--profile", TEST_PROFILE]
        subprocess.run(mk + ["--t", "15", "--roi", "0.18", "0.78", "0.64", "0.13", "--id", "boss_bar",
                             "--event", "boss_bar", "--min-duration", "1.0"], check=True)
        # boss bar uses the automatic foreground mask; banners use the multi-sighting stable mask
        subprocess.run(mk + ["--t", "25", "--also-t", "24.4", "--also-t", "26.6", "--roi", "0.15", "0.40",
                             "0.70", "0.20", "--id", "died", "--event", "death"], check=True)
        subprocess.run(mk + ["--t", "63", "--also-t", "62.4", "--also-t", "64.6", "--roi", "0.12", "0.40",
                             "0.76", "0.20", "--id", "felled", "--event", "victory"], check=True)
        p = PROFILES_DIR / TEST_PROFILE / "profile.json"
        spec = json.loads(p.read_text())
        spec["encounters"] = [{"name": "boss fight", "while": "boss_bar", "gap_fill_s": 4,
                               "inside": {"death": "boss_death"},
                               "ends_with": {"victory": {"within_s": 15, "kind": "boss_victory"}},
                               "unconfirmed_end_kind": "boss_encounter_end"}]
        p.write_text(json.dumps(spec, indent=2))
        detect.analyze(FIX / "synthetic_markers.mp4", load_config(), TEST_PROFILE, "none", True)
        cls.cands = json.loads((ROOT / "analysis/synthetic_markers/candidates.json").read_text())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(PROFILES_DIR / TEST_PROFILE, ignore_errors=True)

    def _kind(self, k):
        return [c for c in self.cands if c["kind"] == k]

    def test_boss_death(self):
        d = self._kind("boss_death")
        self.assertEqual(len(d), 1, self.cands)
        self.assertAlmostEqual(d[0]["peak_t"], fx.MARKER_TRUTH["boss_death"], delta=1.0)

    def test_boss_victory_confirmed_by_banner(self):
        v = self._kind("boss_victory")
        self.assertEqual(len(v), 1, self.cands)
        self.assertAlmostEqual(v[0]["peak_t"], fx.MARKER_TRUTH["boss_victory"], delta=1.0)
        self.assertEqual(v[0]["confidence"], "high")
        self.assertAlmostEqual(v[0]["encounter_start"], 40.0, delta=1.0)
        self.assertGreaterEqual(v[0]["start"], 37.0)  # clip doesn't start before the fight (+3 s intro)

    def test_no_unconfirmed_or_stray_events(self):
        self.assertFalse(self._kind("boss_encounter_end"))
        self.assertFalse(self._kind("death"), "death inside an encounter must become boss_death only")
        self.assertFalse(self._kind("victory"))

    def test_profile_events_rank_above_generic(self):
        self.assertEqual(self.cands[0]["kind"], "boss_victory")


class TestElevenLabsOffline(unittest.TestCase):
    """ElevenLabs connector with the network mocked out - no API key or credits needed."""

    def setUp(self):
        import os
        import tts
        self.tts, self.os = tts, os
        self._old = os.environ.get("ELEVENLABS_API_KEY")
        os.environ["ELEVENLABS_API_KEY"] = "test-key-not-real"
        self.cache_dir = tts.CACHE
        tts.CACHE = ROOT / "tests" / "fixtures" / "tts_cache"
        shutil.rmtree(tts.CACHE, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(self.tts.CACHE, ignore_errors=True)
        self.tts.CACHE = self.cache_dir
        if self._old is None:
            self.os.environ.pop("ELEVENLABS_API_KEY", None)
        else:
            self.os.environ["ELEVENLABS_API_KEY"] = self._old

    def _engine(self, mp3: bytes):
        el = self.tts.engine("elevenlabs")
        calls = []

        def fake_request(path, body=None):
            calls.append(path)
            if path == "/voices":
                return json.dumps({"voices": [{"name": "Narrator - Deep, Resonant", "voice_id": "A" * 20,
                                               "category": "premade"}]}).encode()
            if "with-timestamps" in path:   # speak() uses the timestamps endpoint: JSON with base64 audio
                import base64
                return json.dumps({"audio_base64": base64.b64encode(mp3).decode(), "alignment": None}).encode()
            return mp3
        el._request = fake_request
        return el, calls

    def test_voice_name_resolution(self):
        el, calls = self._engine(b"")
        self.assertEqual(el.voice_id("Adam"), "pNInz6obpgDQGcFmaJgB")   # premade: no API call
        self.assertEqual(calls, [])
        self.assertEqual(el.voice_id("Narrator"), "A" * 20)              # looked up on the account
        self.assertEqual(el.voice_id("B" * 20), "B" * 20)
        with self.assertRaises(SystemExit):
            el.voice_id("Nobody")

    def test_speak_decodes_and_caches(self):
        from gc_common import ffbin
        mp3 = subprocess.run([ffbin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                              "sine=frequency=440:duration=1", "-f", "mp3", "pipe:1"],
                             capture_output=True, check=True).stdout
        el, calls = self._engine(mp3)
        a1, sr = self.tts.speak(el, "Seven bosses.", "Narrator", 1.0)
        self.assertEqual(sr, 24000)
        self.assertAlmostEqual(len(a1) / sr, 1.0, delta=0.1)
        n_tts = sum(1 for c in calls if c.startswith("/text-to-speech"))
        a2, _ = self.tts.speak(el, "Seven bosses.", "Narrator", 1.0)   # same line again: must come from cache
        self.assertEqual(sum(1 for c in calls if c.startswith("/text-to-speech")), n_tts)
        self.assertEqual(len(a1), len(a2))

    def test_missing_key_gives_clear_error(self):
        self.os.environ.pop("ELEVENLABS_API_KEY", None)
        real_root = self.tts.ROOT
        self.tts.ROOT = ROOT / "tests" / "fixtures"   # a folder with no .env, so a real key is never read
        try:
            with self.assertRaises(SystemExit) as cm:
                self.tts.engine("elevenlabs")
        finally:
            self.tts.ROOT = real_root
        self.assertIn(".env", str(cm.exception))


class TestEvaluation(unittest.TestCase):
    L = [dict(recording="r", start=100, end=120, event="boss_victory", interesting=True, notes=""),
         dict(recording="r", start=300, end=310, event="death", interesting=True, notes=""),
         dict(recording="r", start=500, end=520, event="menu", interesting=False, notes="")]

    @staticmethod
    def c(rank, peak, pre=5, post=5):
        return dict(rank=rank, peak_t=peak, start=peak - pre, end=peak + post)

    def test_duplicates_do_not_inflate_recall_or_precision(self):
        cands = [self.c(1, 110), self.c(2, 112), self.c(3, 115)]
        r = evaluate.score_recording(cands, self.L, 10, 1.0)
        self.assertEqual(r["recall_at_k"], 0.5)
        self.assertEqual(r["hits"], 1); self.assertEqual(r["duplicates"], 2)
        self.assertAlmostEqual(r["precision_at_k"], 1 / 3, places=3)

    def test_negatives_and_unlabelled(self):
        cands = [self.c(1, 305), self.c(2, 510), self.c(3, 800)]
        r = evaluate.score_recording(cands, self.L, 10, 0.5)
        self.assertEqual(r["outcome_by_rank"], ["hit", "known_negative", "unlabelled"])
        self.assertEqual(r["fp_per_hour"], 4.0)
        self.assertEqual(r["recall_by_event"], {"boss_victory": "0/1", "death": "1/1"})

    def test_top_k_cutoff(self):
        cands = [self.c(1, 800), self.c(2, 110)]
        self.assertEqual(evaluate.score_recording(cands, self.L, 1, 1.0)["recall_at_k"], 0.0)
        self.assertEqual(evaluate.score_recording(cands, self.L, 2, 1.0)["recall_at_k"], 0.5)

    def test_zero_candidates(self):
        r = evaluate.score_recording([], self.L, 10, 1.0)
        self.assertEqual(r["recall_at_k"], 0.0); self.assertIsNone(r["precision_at_k"])

    def test_overlap_rule(self):
        lab = dict(start=100, end=120)
        self.assertTrue(evaluate.hits(dict(peak_t=125, start=115, end=130), lab))   # 5 s overlap
        self.assertFalse(evaluate.hits(dict(peak_t=130, start=119, end=140), lab))  # 1 s overlap only
        self.assertTrue(evaluate.hits(dict(peak_t=101, start=90, end=102), lab))    # peak inside


if __name__ == "__main__":
    unittest.main()
