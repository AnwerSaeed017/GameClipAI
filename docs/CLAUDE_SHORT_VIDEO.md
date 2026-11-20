# Claude guide: making a YouTube Short

How to build a vertical Short (1080×1920, 45–58 s) from the user's PS5 sessions, as done for
"GTA 6 First Day: Gunfight, Plane Crash… and Painting a Room". Read [CLAUDE_LONG_VIDEO.md](CLAUDE_LONG_VIDEO.md)
for the shared rules (no cutscenes, no radio music, confirm scripts before ElevenLabs, review like a viewer).

## Formats that work
- **First-day / session recap:** 10–12 beats of ~4–5 s each (hook, boat, combat, fail, success, night, a
  vehicle, house, build, ending). Contrast is the hook ("gunfight, plane crash… and painting a room").
- **Transformation:** before → steps → reveal → question (the rusty-pickup workshop Short).
- Avoid clips where in-game characters talk over the whole beat; if needed, lower them with `game_audio`.

## Layout rules (from the user's feedback)
- **Full 16:9 frame, nothing cropped at the sides:** gameplay window on a blurred copy of itself.
  Do **not** use `fg_scale` > 1 (it cut the left/right corners and the user objected).
- **Open with a centred title on blur**, then cut to the sharp gameplay: a 1.8 s `blur_only` segment of the
  same footage just before the first clip, with the title in the middle (`y` ≈ 800), no top caption.
- **Captions match the subtitles:** Montserrat Black, white text, pink `#FF4FA0` accent, dark-purple outline
  `#19052D`; CTA as a pink pill. Use the per-caption overrides in `captions.py`.
- **Hard cuts** (`fade_in`/`fade_out` 0); only the last segment fades out (0.4 s).
- Pick bright clips; check night footage on tone-mapped frames (a dark stretch reads as black on a phone).
  Lift night/interior segments with `"gamma": 1.3–1.5` (optional `"brightness"`); compare a before/after frame first.
- Cold open on the funniest line over gameplay (no blurred card) when the story has a payoff to tease; quote
  in-game dialogue as big captions copied exactly from the game's subtitles.

```json
{"segments": [
  {"src": "input/S1.webm", "in": 2153.2, "out": 2155, "blur_only": true, "fade_in": 0, "fade_out": 0,
   "texts": [{"lines": ["OUR FIRST DAY", "IN *GTA 6*"], "style": "hook", "start": 0, "end": 1.8, "y": 800,
              "size": 120, "font": "Montserrat-Black.ttf", "hi": "#FF4FA0", "outline": "#19052D"}]},
  {"src": "input/S1.webm", "in": 2155, "out": 2158.5, "fade_in": 0, "fade_out": 0},
  {"src": "input/S2.webm", "in": 778, "out": 784, "fade_out": 0.4, "game_audio": 0.6,
   "texts": [{"lines": ["WHAT SHOULD I", "*TRY NEXT?*"], "style": "cta", "start": 2.4, "end": 6.0, "y": 300,
              "size": 72, "font": "Montserrat-Black.ttf", "pill": "#FF4FA0", "hi": "#FFFFFF"}]}],
 "subtitles": {"size": 116, "margin_v": 470, "max_words": 4, "max_chars": 16, "outline": 7, "shadow": 3}}
```
Short EDL times are **seconds** (not mm:ss). `render_short.py` has no `audio_from`: skip clips with radio music.

## Subtitles (on for Shorts)
- Word-synced karaoke from ElevenLabs timings: white words, active word pink with a small pop.
- `max_chars: 16` at size 116 keeps every phrase on one line (a 4-word phrase once ran off-screen).
  Long phrases wrap instead of clipping. "GTA 6"/"GTA 5" are always kept together.
- Check a close-up strip of the subtitle area (crop y 1250–1670) at 3 fps for the first 10 s.

## Script (first person, excited, human)
- Open like a creator who just started playing: "Okay guys… the wait is over. I just downloaded GTA 6… and
  it's already blown my mind."
- One short line per beat, punchy, often a reaction: "First job? It got loud. Fast." / "Then I tried to escape…
  in a seaplane." / "Yeah… no." (lands just before WASTED) / "Second try… okay, NOW we're flying!"
- Chain the last beats: "Borrowed a truck…" "…painted a whole room…" "…and turned a rusty pickup into this!"
- End on a question, and make the CTA card use the **same words** as the narration ("I" vs "we").
- Measure every line against the timeline before rendering; no overlaps (shift `at` if a long opener spills).

## Review checklist
- QC: duration as planned, −14 LUFS, true peak ≤ −1 dBTP, no black intervals.
- 2-fps sheet of the whole Short: edges not cropped, subtitles fit, title centred, joke timing, CTA visible,
  no dark or black dips at cuts.
- Render time ~6–10 min; only segments whose spec, subtitles or code changed re-render.

## Upload
- Title ends with `#shorts`; description in the same voice as the narration; hashtags `#GTA6 #GTAVI #Shorts`.
- Shorts thumbnails: YouTube picks a frame; choose one in the YouTube mobile app while uploading
  (Studio on desktop doesn't offer a custom image for Shorts).
