# Claude guide: making a long-form YouTube video

How to turn the user's raw PS5 sessions into a finished long video (8–15 min), the way the GTA 6
"13 Years of Waiting… Our First Hours" video was made. Read [SETUP.md](SETUP.md) for commands and
[PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) for architecture first.

## What the user expects
- **Real gameplay only.** No story cutscenes, no car-radio music (copyright), nothing narrated that isn't on screen.
- **A human channel voice.** Narration talks *to the audience* like a creator, not a documentary. Reactions,
  opinions, jokes, comparisons. Never describe every scene; let gameplay breathe for long stretches.
- **Look at it like a viewer.** Check frames before choosing a clip, and review the whole render before
  handing it over. The user's rule: "don't make mistakes… visualize and experience it."
- **Confirm the script before spending ElevenLabs credits** (hard rule in CLAUDE.md). Show it grouped by
  chapter as `[what's on screen] → line`.

## Workflow
1. **Ingest and detect.** `scripts/detect.py` on each session (GTA 6 profile: `profiles/gta6`, WASTED template).
2. **Review the whole session.** Timeline pages from detect, then dense sheets with `scripts/inspect_range.py`
   for any moment you plan to use. Write a catalogue (`exports/long/<name>/PLAN.md`): timestamp, what's
   visible, audio problems (radio, dialogue, profanity), usable or not.
3. **Structure (chapters).** Cold open (3 teaser shots) → title card → 6–9 chapters → "first impressions" →
   end card. Every chapter must be ≥ 10 s; the first starts at 00:00 (render_long enforces it).
4. **Write the EDL** (`exports/long/<name>/edl.json`). Times may be `mm:ss`.
5. **Dry run:** `render_long.py --plan` writes `timeline.md` (output time of every segment).
6. **Script → approval → voice.** After the user approves: `scripts/narrate.py <edl>` generates every line once
   (cached in `.cache/tts`) and writes `narration_check.md` with measured lengths, gaps and overlaps.
   Fix every OVERLAP / RUNS PAST END before rendering.
7. **Render alone** (never two renders at once): `scripts/render_long.py <edl>` in the background.
   4K30 at 45 Mbps takes ~2–2.5 h for 12 min on the i7-1165G7 (the HDR VP9 decode is the cost, not encoding).
8. **Review** (checklist below), fix, re-render (segment cache only re-renders changed segments; any code
   change invalidates all of them via `code_fingerprint()`).
9. **Upload notes:** title, description with chapters from `chapters.txt`, tags, pinned comment, settings.

## EDL essentials (long)
```json
{"resolution": [3840, 2160], "fps": 30, "bitrate": "45M", "encoder": "qsv",
 "subtitles": {"enabled": false},
 "segments": [
  {"src": "input/S1.webm", "in": "35:55", "out": "36:05", "fade_in": 0, "chapter": "13 Years Later"},
  {"src": "input/S1.webm", "in": "35:20.5", "out": "35:28", "speed": 1.5,
   "zoom": [{"at": 2.9, "dur": 2.2, "scale": 1.12, "ramp": 0.25}]},
  {"type": "card", "dur": 3.5, "lines": ["GTA 6", "13 YEARS OF WAITING…", "OUR FIRST *HOURS*"],
   "src": "input/S1.webm", "t": "36:05", "size": 120},
  {"src": "input/S2.webm", "in": "03:40", "out": "04:06",
   "audio_from": {"src": "input/S2.webm", "in": "02:44"}}
 ],
 "narration": {"engine": "elevenlabs", "voice": "HfRP3cIhYLmeNHeTvkWK",
   "elevenlabs": {"model": "eleven_v3", "stability": 0.5, "similarity": 0.8, "style": 0.0}, "speed": 0.95,
   "lines": [{"seg": 0, "at": 0.3, "text": "…"}, {"seg": 4, "src_at": "18:10", "text": "…"}]},
 "sfx": [{"seg": 1, "at": 2.9, "type": "hit", "gain": -8}]}
```
- `src_at` anchors a line to a **source** timestamp, so speed ramps and length changes never desync it.
  Use `at` (seconds into the segment) only for cards and fixed shots.
- `audio_from` keeps the visuals but swaps the audio for a clean stretch (used where the car radio played).
- `game_audio` (0–1) lowers the game sound under a segment.
- Narration subtitles are **off** for long videos (the game already shows its own subtitles; the user found
  both together cluttered). They stay on for Shorts.

## Script rules (what worked)
- **Intro = excitement, talking to viewers**, then the title card. Example: "Guys… it's finally here. GTA 6 is
  out. Thirteen years after GTA 5… and we're finally playing it." → "And honestly? Our first hours did not go
  to plan." → "At one point… we were painting walls." → card: "So here they are. Our first hours in GTA 6."
- ~3 lines per minute, not 5. Short reactions ("That colour, though…", "Come on… lift off!",
  "Thirteen years later… still wasted.") beat descriptions.
- **Comparisons with the previous game**, researched first (WebSearch: reviews, Digital Foundry-style
  analyses). Keep only claims that are well supported and tie each one to what's on screen (water, lighting,
  nights, hotwire mini-game vs GTA 5's automatic animation, home renovation, the Dodo seaplane).
  Drop speculation (e.g. "ray tracing").
- Verify on-screen facts on frames before narrating them (prompt text, who the character is: the "Key Lento
  truck" is Jason, and the prompt confirmed hotwiring).
- Land a joke **before** the payoff (the line ends just as WASTED appears).
- Ending: honest verdict in 3–4 short lines, then one question for comments.

## Review checklist (do all of it before handing over)
- Frames every 6 s across the whole video (tile into 8×5 sheets with ffmpeg, then read them).
  A black tile on a 6-s grid often lands exactly on a cut fade: confirm with
  `blackdetect=d=0.05:pix_th=0.10` on that range before worrying.
- Title card visible, chapters embedded: `ffprobe -show_chapters`.
- QC from the render (`output_metadata.json`): duration = expected, −14 LUFS, **true peak ≤ −1 dBTP**.
  If the peak is too high, fix the audio only, copying the video:
  `ffmpeg -i in.mp4 -map 0:v -map 0:a -map_chapters 0 -map_metadata 0 -c:v copy -af "aresample=192000,alimiter=limit=0.76:attack=1:release=60:level=false,aresample=48000" -c:a aac -b:a 320k -movflags +faststart out.mp4`,
  then re-measure with `ebur128=peak=true`.
- The only long black should be the game's own WASTED fade.
- Tell the user about in-game profanity (GTA subtitles/dialogue) so they decide on monetisation.

## Thumbnail
The user wants **illustrated thumbnails generated in ChatGPT** (characters, bright GTA-style art); Claude writes
the prompt and finishes the file (1920×1080 JPG under 2 MB), it doesn't composite its own. Never use official
logos (the "GTA VI" logo version was rejected for that reason).

## Gotchas on this Windows machine
- Use the **Write/Edit tools** for files; PowerShell `Set-Content` adds a BOM and `` `n `` inside single quotes is
  literal (it once broke a JSON file).
- `drawtext` without a fontfile can hang on Windows font scanning; label frames another way.
- libass uses `assets/fonts/fonts.conf` (set automatically by `subtitles.py`).
- Load the PATH in PowerShell first: `$env:Path = [Environment]::GetEnvironmentVariable("Path","User") + ";" + [Environment]::GetEnvironmentVariable("Path","Machine")`.
