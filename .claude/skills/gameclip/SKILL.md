---
name: gameclip
description: Process a new PS5 gameplay session for the user's YouTube channel - copy from the Kingston USB, run the GameClip AI detector, have Claude review the whole session and the candidates, propose categorized clips (Shorts, long-form segments, compilation material), collect the user's yes/no, and render approved clips. Use when the user says they recorded a session, copied clips, plugged in the USB, or asks to find/analyze good moments.
---

# GameClip session workflow

The user does NOT label timestamps. Claude finds and characterizes the moments; the user only approves or rejects proposals. Their decisions become the ground truth.

All commands run from `D:\content-generation\GameClipAI` with `.venv\Scripts\python.exe` (shown as `py>`). Refresh PATH first in PowerShell:
`$env:Path = [Environment]::GetEnvironmentVariable("Path","User") + ";" + [Environment]::GetEnvironmentVariable("Path","Machine")`

## 1. Ingest
- The USB is the Kingston drive (usually `E:`), with files in `E:\PS5\CREATE\Video Clips\<Game>\`.
- Copy into `input\` with clean names: `<game_slug>_<yyyymmddhhmmss>.<ext>`, for example `gta6_20261119203000.webm`. Never move or modify the originals.
- Skip byte-identical duplicates (the PS5 sometimes exports `_1` copies).
- Add rows to `dataset\recordings.csv` (recording, game, genre, capture_type, duration_s). capture_type is `continuous_gameplay`, `trophy_capture` (15 s with the PS5 trophy overlay), `cutscene` or `unknown`.
- Delete from the USB only when the user asks, and only after verifying file hashes against `input\`.

## 2. Analyze
- `py> scripts\probe.py input\<file>` for format and HDR.
- `py> scripts\detect.py input\<file>`, adding `--profile <game>` if `profiles\<game>\profile.json` exists. `--hwaccel auto` is the default.
- `py> scripts\overview.py input\<file> --every 10` builds whole-session timeline pages (30 frames = 5 min per page).

## 3. Claude review (the core step)
- Read the timeline pages for the WHOLE session first, then the candidate contact sheets (`analysis\<rec>\sheets\`). Read the `_peak.jpg` frames only when a detail matters.
- Look for good moments the detector missed. Mark them `found_by: claude_timeline`.
- To refine a moment, make a denser contact sheet of just that range with `detect.contact_sheet`.
- Pro usage budget: a 30-min session at `--every 10` is about 6 pages, plus the top ~15 sheets. Don't read every peak frame.
- Write `analysis\<rec>\review.json` (list) and a short `review.md`. For each moment record:
  - `start`, `end`, `event` (combat, boss_kill, death, chase, crash, stunt, funny, exploration, mission_complete, cutscene...)
  - `what_happens`: describe ONLY what is visible. Never invent outcomes. A boss bar still up means no victory.
  - `event_confidence`: confirmed / likely / uncertain
  - `entertainment`, `clarity`, `standalone`: 1-5 each
  - `best_use`: short / long_segment / compilation:<theme> / reject
  - `suggested_cut`: in/out seconds, with the hook moment first if possible
  - `found_by`: detector / claude_timeline
- Group the proposals into: Shorts (self-contained, 15-45 s), long-form segments (a story arc: build-up, struggle, payoff), and compilation themes (for example "police chases", "deaths", "boss kills").

## 4. User decision
Present a compact numbered list (time, one line, proposed use). The user answers yes or no per item, or "all except 3, 7". Record the answers to `dataset\ground_truth.csv`: approved = `interesting=true`, rejected = `false`, with the event name. Then run `py> scripts\evaluate.py` to track detector recall and precision against Claude-plus-user truth.

## 5. Produce
- Write an EDL like `exports\shorts\two-worlds\edl.json`, then `py> scripts\render_short.py <edl.json>`. It renders 1080x1920 with a blurred-background layout, Quick Sync encoding, loudness at -14 LUFS, and a QC report.
- Long-form: `py> scripts\render_long.py <edl.json>` renders 1920x1080 with title cards, embedded chapters plus chapters.txt, and Kokoro narration placed at times, with the game audio ducking under it.
- Captions (scripts/captions.py) are drawn as PNGs and slide in. Styles:
  - `hook`: huge, for 0-3 s. Starting at 0 makes it appear instantly.
  - `caption`: main text.
  - `label`: yellow pill for the game name or "BOSS 3 / 7".
  - `cta`: red pill for the call to action.
  - Wrap ONE key word per line in `*stars*` to turn it yellow. Highlight the payoff word, not filler.
  - Shorts: keep captions between y=280 and y=1500 (YouTube UI covers the bottom ~350 px and the right edge).
  - Long-form: put captions at the top (y 40-100) so they never cover the game's boss bars and HUD.
- Optional narration, two engines:
  - `--engine kokoro`: local, free, calm.
  - `--engine elevenlabs`: expressive. The user has a Creator plan. Key in `.env`; never print it.
  - Example: `py> scripts\tts.py file script.txt --engine elevenlabs --voice <name> --out exports\narration\x.wav`. In long-form EDLs use `"narration": {"engine": "elevenlabs", ...}`.
  - ElevenLabs costs credits, so show the user the final script before generating. Identical lines are cached.
- Never publish or upload. The user decides.

## GTA 6 formats (from docs\GTA6_Shorts_Launch_Plan.pdf)
Classify GTA moments into the channel's formats as well as event types:
- test/experiment (question hook, answer at the end)
- hidden_detail / throwback
- gta5_vs_gta6
- tips
- cinematic (golden hour, scenery; loops well)
- ranking
- fails (compilation)
- npc_chatter (compilation)
- region_tour / timelapse
- challenge

Lengths: Shorts 20-45 s; 60-90 s TikTok cuts of the strongest; weekly long-form 8-12 min ("N things we tested this week") built with render_long.py, with chapters.

Before a GTA session, offer the user a 3-5 item session brief (tests to try) so test-format content actually gets recorded. Offer 3 hook options per clip and let the user pick: the creative decisions must be theirs (YouTube inauthentic-content policy).

## Content and rights rules
- GTA VI: no cutscenes, endings, story dialogue or mission reveals (Rockstar takes those down). No graphic violence in the first 7 s or on the cover (YouTube ad rules); this overrides "open on the action". No NPC-massacre shock compilations. GTA 6 is 30 fps; the renderers follow the source fps.
- Licensed radio music triggers Content ID claims, so flag clips with radio playing. Monetization under Rockstar's policy is unverified, so remind the user if they're about to monetize.
- No other creators' footage. No background music that isn't royalty-free.
- Captions and narration must match what is on screen.
