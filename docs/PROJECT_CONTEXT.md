# GameClip AI: project context

This is the briefing a new contributor, or their Claude Code, needs to work on this project without re-deriving it. It covers what the project is for, how it's built and why, what real footage taught us, the bugs already fixed, what's validated, and what's next. Setup and commands are in [SETUP.md](SETUP.md).

## 1. Goal and constraints

- Produce **YouTube Shorts and long-form videos** from **PS5 gameplay recordings** for a monetized gaming channel. GTA 6 is the main focus from launch day (19 Nov 2026), alongside soulslike and action games (Elden Ring, Sekiro, Ghost of Yōtei, Onimusha, Resident Evil, Spider-Man 2, Tekken 8, Clair Obscur: Expedition 33...).
- **No commentary.** Recordings contain only game audio, so speech-based clippers (OpusClip and similar) don't fit.
- **Local and free.** It was built specifically to avoid Eklipse and other cloud clippers, with no uploads and no subscriptions. Runs on a mid-range Windows laptop (developed on an i7-1165G7 with Intel Iris Xe and no NVIDIA GPU).
- **The user doesn't label timestamps.** Claude reviews each session and proposes clips; the user only approves or rejects. Those decisions become the ground truth for measuring the detector.
- **Human creative control is a hard requirement.** YouTube's inauthentic-content policy demonetizes fully automated, templated channels. Claude proposes options (for example 3 hooks); the person picks.

## 2. Architecture (data flow)

```
PS5 capture (WebM VP9 / MP4 H.264, often HDR) -> USB (exFAT) -> input/
  probe.py       format, HDR, decoder benchmark
  detect.py      ONE ffmpeg decode at 320x180 @ 5 fps (+ game audio @ 16 kHz, in parallel)
                 -> per-frame signals: audio rise over a 30 s rolling median (dB), spectral-flux onsets,
                    residual motion after phase-correlation camera compensation, camera speed,
                    histogram cuts, static/black detection, optional HUD template scores
                 -> robust z-scores + ABSOLUTE floors -> peaks -> merge -> context windows
                 -> candidates.json/csv, contact sheets, peak frames, signals.png, timeline.csv
  overview.py    whole-session timeline pages (Claude reviews ALL of it, not only the candidates)
  Claude review  -> moments classified (event, entertainment/clarity/standalone 1-5, best use:
                    short / long_segment / compilation:<theme>) -> user yes/no
  EDL (JSON)     -> render_short.py (1080x1920) / render_long.py (1920x1080 + chapters + narration)
                    captions.py draws caption PNGs; tts.py = Kokoro narration
  evaluate.py    detector vs approved/rejected labels: recall@K, precision@K, FP/hour
```

## 3. Key design decisions and why

| Decision | Why |
|---|---|
| Decode once at low resolution; draw sheets and peak frames from the original | Full-res analysis of 4K HDR is far too slow on a laptop. HUD reading needs more pixels, so a profile can raise the analysis resolution (e.g. 640x360) |
| Camera-motion compensation (phase correlation) | In 3rd/1st-person and racing games, camera pans dominate raw frame differences. On test video, a fast pan gave residual ~1 vs ~35 for real action |
| 1-frame residual spikes are median-filtered out | An undetected hard cut looks like a single-frame spike; real action lasts longer |
| Absolute floors on top of relative z-scores | Without them a boring recording still yields "top candidates". A boring test video correctly returns 0 |
| Audio = rise over the rolling median + onsets | Sustained loud music (boss themes, radio) isn't an event; sudden hits are |
| Game profiles are pure data (`profiles/<game>/profile.json`) | No genre logic in code. Encounter rules (`while` marker on screen, `inside` → kind, `ends_with` → confirmed outcome) cover boss fights, fighting-game rounds, GTA wasted/mission screens |
| Masked template matching (mask from 2+ sightings of the same banner) | Unmasked templates failed (~0.6 score) when the scene behind a banner changed; masked ones match reliably |
| A victory needs a confirming marker | A boss bar disappearing isn't a win (it also vanishes on death or in cutscenes). Unconfirmed endings are tagged `low` confidence |
| Claude reviews whole-session timelines | Without user labels, misses can only be found by looking at everything |
| Captions are PIL-drawn PNGs, not FFmpeg drawtext | Needed keyword highlights, outlines, shadows, rounded pills and slide-in. drawtext can't do per-word colour |
| Renderers follow source fps | GTA 6 is 30 fps; converting to 60 only duplicates frames |
| `--hwaccel auto`: GPU decode only for 10-bit/HDR | Measured on real PS5 files: GPU decode + 5 fps drop on the GPU is 1.7x faster for HDR VP9; the CPU is faster for 8-bit H.264 |
| Two narration engines behind one interface (`tts.engine(name)` / `tts.speak`) | Kokoro (Apache 2.0, free, offline, ~1x realtime on CPU) sounds calm; the user found it not exciting enough. ElevenLabs (the user has a Creator plan) is the expressive option. The EDL picks the engine per video |
| ElevenLabs via the REST API, standard library only, key in `.env`, output cached by hash | No extra dependency. The key never touches code or git. Re-rendering an edit never re-spends credits |

## 4. What real footage taught us (Nov 2026)

- **PS5 WebM with HDR** = VP9 10-bit, **1920x1088** (8 padding rows, cropped automatically), PQ/BT.2020 (tone-mapped automatically). MP4 captures were H.264 8-bit SDR.
- **15-second clips are usually PS5 trophy auto-captures:**
  - The PS5 trophy overlay (curved frame, trophy banner, "Trophy earned" popup) is baked into roughly the first 5 s.
  - These clips are pre-selected moments, so they can't measure detection in long sessions.
  - In soulslikes, trophy clips are often boss kills ("GREAT ENEMY FELLED" / "LEGEND FELLED" ~7 s in).
  - Turning on trophy auto-capture is a free highlight source.
- **Cutscenes score as "action"** (a 134 s Onimusha cutscene topped the candidates). Each game shows a "Skip" prompt during cutscenes, which makes a good profile marker. Cutscenes must be flagged: Rockstar takes down cutscene uploads.
- **Always verify what a clip shows before captioning it.** A clip assumed to be "Takezo defeated" (Ghost of Yōtei) only showed the aftermath, and the "Spider" clips showed the fight with the boss bar still up, not a win. Contact sheets and peak frames exist for this.
- **Boss and trophy names** can be read from the trophy notification (top right) at full resolution. Don't guess them.

## 5. Bugs already found and fixed (don't reintroduce)

1. **OpenCV 5 `cv2.phaseCorrelate` modifies its input arrays in place** (applies the window). Every later frame difference was corrupted. Fix: pass `.copy()`.
2. **FFmpeg 9 + `-hwaccel qsv`/`d3d11va` keep frames in GPU memory.** Filters need `hwdownload,format=nv12|p010le`. Put `fps=` BEFORE `hwdownload`, otherwise every 60 fps frame is copied back. `vpp_qsv` failed with "Could not create the texture" on this Iris Xe driver, so it isn't used.
3. **`fade=...:d=0` isn't "no fade":** FFmpeg falls back to a 25-frame default. Omit the filter when the duration is 0.
4. **FFmpeg concat lists resolve relative paths from the list file's folder.** Write absolute paths.
5. **PowerShell 5 `Set-Content -Encoding utf8` writes a BOM,** which breaks `json.loads`. All EDL readers use `utf-8-sig`.
6. **Synthetic test video artifact:** a texture that wraps around during a pan looks like an invisible cut. The fixture texture is now wide enough not to wrap.

## 6. Validated vs not validated

| Validated | Not yet validated |
|---|---|
| 22 tests: known events found at the right time, fast camera pans ignored, static/black screens filtered, boring video → 0 candidates, no-audio and HDR inputs, profile encounter rules (boss death vs confirmed victory), evaluation maths (duplicates can't inflate recall) | **Detection accuracy on long, uncut real sessions.** No 20–40 min recording has been evaluated yet; the first GTA 6 sessions are meant to be that test |
| All decoders produce identical candidates; HDR tone mapping looks natural on Onimusha | Thresholds in `config/default.json` are starting values, not tuned |
| Shorts and long-form renders pass QC (size, fps, −14 LUFS, no black frames, chapters, narration ducking) | No game profile exists yet for any real game (only the format example) |
| Kokoro narration works with the full and quantized models. The ElevenLabs connector passes offline tests (voice lookup, MP3 decode, caching, missing-key error) | ElevenLabs hasn't been run against the live API in this repo yet (no key was configured when it was written). The channel voice (engine + voice) isn't chosen |

## 7. Open work / roadmap

1. **First real GTA 6 sessions:** run detect + overview, Claude review, user approvals, then `evaluate.py`. Tune thresholds from evidence, not guesses.
2. **GTA 6 profile** from real footage: wasted/busted, mission passed, wanted-level stars, cutscene "Skip" prompt (to exclude).
3. **Cutscene and PS5 trophy-overlay recognition** as universal markers, so they don't score as action.
4. **Smart 9:16 crop layout** (zoomed on the action) next to the current full-frame-on-blur layout.
5. **Narration:** run ElevenLabs live with the Creator-plan key, pick the channel voice and settings, and try `eleven_v3` audio tags for hype lines. Keep Kokoro for drafts and calm videos. XTTS-v2 is **non-commercial**, so don't use it.
6. **Formats from `GTA6_Shorts_Launch_Plan.pdf`:** "We tested..." experiments, hidden details, tips, cinematic, rankings, fails/NPC compilations, 60–90 s TikTok cuts, weekly 8–12 min long-form with chapters. Offer a 3–5 item session brief before recording so test-format footage exists.

## 8. Working conventions for Claude on this repo

- **Never caption or narrate an event that isn't visible.** Check contact sheets and peak frames first.
- **Never commit media, models, `input/`, `analysis/`, personal datasets or secrets** (`.gitignore` covers these). Commit EDL examples, not renders.
- **Never read out, print or log the ElevenLabs key.** It lives only in `.env`. ElevenLabs generations cost credits: finalize scripts before rendering, and don't loop over voices without asking.
- Originals in `input/` are read-only. Outputs go to `analysis/` and `exports/`.
- Run `py -m unittest discover -s tests` after code changes. Add a synthetic test for new detector behaviour.
- On Windows, refresh PATH in new PowerShell sessions if FFmpeg was just installed. Prefer the Write/Edit tools over PowerShell for JSON (BOM, see bug 5).
- **Image review costs usage.** Read whole-session timeline pages (30 frames each) and the top contact sheets. Don't open every peak frame.
- **GTA 6 rules:**
  - no cutscenes, endings or story dialogue
  - no graphic violence in the first 7 s or on the cover
  - radio off; flag clips where it plays
  - no NPC-massacre compilations
  - Rockstar's position on monetized video is unconfirmed, so remind before monetizing.
