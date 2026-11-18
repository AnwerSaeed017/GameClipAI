# GameClip AI: setup and developer guide

GameClip AI is a local toolkit for gameplay video. It finds highlight moments in PS5 recordings and renders YouTube Shorts (9:16) and long-form videos (16:9), with styled captions, chapters and optional AI narration. Everything runs on your own computer, with no cloud services and no subscriptions.

It is built for no-commentary footage (game audio only) and works with any game. It currently targets **GTA 6**, Elden Ring, Ghost of Yōtei and Onimusha.

For background on why things are built this way, what's validated and what's next, see [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md). Claude Code picks this up automatically through `CLAUDE.md`.

---

## 1. Requirements

| | |
|---|---|
| OS | Windows 10/11. The caption renderer uses fonts from `C:\Windows\Fonts` |
| Python | 3.12 |
| FFmpeg | 7 or newer, **full build** (needs `zscale`/`libplacebo` for HDR, and `h264_qsv` for Intel GPU encoding) |
| Disk | ~1 GB for tools and models, plus space for your recordings |
| GPU | Optional. Intel Quick Sync is used if present. Otherwise pass `--encoder x264` to the renderers (CPU) |

Developed and tested on an i7-1165G7 laptop (Intel Iris Xe), 24 GB RAM, FFmpeg 9.0.2 (gyan.dev full build).

## 2. Install

```powershell
# 1. FFmpeg (full build) - extract it anywhere and put its bin folder on your PATH
#    https://www.gyan.dev/ffmpeg/builds/  -> "ffmpeg-release-full"
ffmpeg -hide_banner -encoders | findstr qsv     # Intel GPU encoders, if you have them

# 2. Get the code
git clone https://github.com/AnwerSaeed017/GameClipAI.git
cd GameClipAI

# 3. Python environment
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 4. Narration model (Kokoro-82M, Apache 2.0: free, commercial use allowed). ~340 MB in total
mkdir models\kokoro
curl.exe -L -o models\kokoro\kokoro-v1.0-hf.onnx https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/resolve/main/onnx/model.onnx
curl.exe -L -o models\kokoro\voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

On a slow connection, add `-C -` to `curl` to resume an interrupted download. A smaller quantized model also works: `onnx/model_quantized.onnx` (~90 MB), saved as `models\kokoro\kokoro-v1.0.int8-hf.onnx`.

```powershell
# 5. ElevenLabs (optional; more expressive narration, needs a PAID plan for monetized videos)
copy .env.example .env      # then paste your key after ELEVENLABS_API_KEY=  (ElevenLabs -> profile -> API Keys)
.\.venv\Scripts\python.exe scripts\tts.py voices                                         # lists your voices (free)
.\.venv\Scripts\python.exe scripts\tts.py samples --engine elevenlabs --voices "Adam"   # costs credits once
```

`.env` is git-ignored. Never commit it, paste the key into chat, or put it in code.

**API key permissions:** when you create the key on elevenlabs.io (API Keys), enable at least **Text to Speech**. Also enable **Voices: Read** if you want `tts.py voices` and voices referenced by name. Without Voices: Read, use a voice ID or one of the built-in names in `PREMADE` in `tts.py` (`Adam` is verified working). Every key has its own credit usage, so one key per person is easier to track.

### Check the install

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v   # 25 tests, ~30 s, generates its own test videos
.\.venv\Scripts\python.exe scripts\tts.py samples             # 7 voice samples -> exports\narration\samples
```

All 25 tests should pass. The ElevenLabs tests mock the network, so they need no key and cost no credits. The tests check that the code behaves correctly on synthetic videos. They don't prove the detector finds good moments in real gameplay.

## 3. Folder layout

```
scripts/        all the code (see section 5)
config/         default.json: every detection threshold in one place
profiles/       optional per-game HUD detection (see section 7). _example/ shows the format
dataset/        *.template.csv: copy to recordings.csv / ground_truth.csv (your own, not committed)
exports/        render plans (edl.json) + output videos. Videos and audio are NOT committed
tests/          unit + synthetic-video tests (fixtures are generated, not committed)
docs/           SETUP.md (this file), PROJECT_CONTEXT.md (decisions, findings, roadmap),
                GTA6_Shorts_Launch_Plan.pdf (content formats, hooks and rules for GTA 6)
CLAUDE.md       entry point Claude Code reads automatically
.claude/skills/gameclip/SKILL.md   the session workflow for Claude Code (/gameclip)

Not committed, created locally: input/ (your recordings), analysis/ (detector output), models/, .venv/
```

## 4. Workflow: from PS5 to videos

**Before you record (PS5 settings):**
- Settings → Captures and Broadcasts: WebM, 4K if you'll make Shorts (a vertical crop of 1080p looks soft).
- Turn the mic and party audio off in captures.
- Use Create → **Start New Recording** for long sessions (20–40 min). Short 15-second "trophy" clips are pre-selected moments and not useful for testing detection.
- **GTA 6:** radio OFF (licensed music gets Content ID claims). Don't use story cutscenes.

**Copy to the PC:** USB drive formatted **exFAT** in Windows (not the PS5's "USB Extended Storage" option). Files are in `PS5\CREATE\Video Clips\<Game>\`. Copy them into `input\` and give them simple names, for example `gta6_20261119203000.webm`.

**Process a session** (`py` = `.\.venv\Scripts\python.exe`):

```powershell
py scripts\probe.py input\gta6_x.webm --bench 30    # format, HDR, fastest decoder
py scripts\detect.py input\gta6_x.webm              # candidates + contact sheets + signal plot -> analysis\gta6_x\
py scripts\overview.py input\gta6_x.webm --every 10 # whole-session timeline pages for review
```

Then review the timeline pages and contact sheets, pick the moments, write an edit plan (EDL, see section 6), and render:

```powershell
py scripts\render_short.py exports\shorts\<name>\edl.json   # 1080x1920 Short
py scripts\render_long.py  exports\long\<name>\edl.json     # 1920x1080 long-form + chapters.txt
```

**With Claude Code:** open the project folder in Claude Code and use the `/gameclip` skill. Claude reviews the whole session, proposes Shorts, long-form segments and compilation moments, you approve or reject, and it renders the approved ones. The full procedure and content rules are in `.claude/skills/gameclip/SKILL.md`.

## 5. Scripts

| Script | What it does |
|---|---|
| `gc_common.py` | Shared helpers: ffprobe, FFmpeg decode pipes, HDR→SDR tone mapping, label loading |
| `probe.py` | Recording format report; `--bench N` times CPU vs Quick Sync vs D3D11 decoding |
| `detect.py` | The detector. Decodes once at 320x180, 5 fps and builds signal timelines: game-audio spikes, on-screen action with camera movement filtered out, static/black screens, scene cuts. Finds peaks and writes `candidates.csv/json`, contact sheets, a peak frame per candidate and `signals.png`. `--profile <game>` adds HUD-based events |
| `overview.py` | `--every N`: the whole recording as pages of timestamped frames. `--prefix`: a mosaic of many short clips |
| `make_template.py` | Builds a game profile from real footage (banner/HUD templates with masks) |
| `evaluate.py` | Scores detector output against `dataset\ground_truth.csv` (recall@K, precision@K, false positives/hour) |
| `captions.py` | Caption graphics (PIL → PNG → animated overlay). Styles: `hook`, `caption`, `label`, `cta`. `*word*` = yellow highlight |
| `render_short.py` | Shorts renderer: full frame over a blurred background, captions, fades, −14 LUFS loudness, quality report |
| `render_long.py` | Long-form renderer: title cards, chapters (embedded + `chapters.txt`), narration with game audio ducking under it |
| `tts.py` | Narration with two engines behind one interface. `--engine kokoro` (local, free, calm) or `--engine elevenlabs` (cloud, expressive, uses your plan's credits). Commands: `voices`, `samples`, `say "text"`, `file script.txt`. ElevenLabs lines are cached in `.cache/tts/`, so re-renders never pay twice |
| `voice_test.py` | Renders one hook in several voices, speeds and audio treatments for comparison |

## 6. Edit plans (EDL)

Renders are driven by a JSON edit plan. Working examples:
- Short: `exports/shorts/two-worlds/edl.json`
- Long-form: `exports/long/elden-ring-7-bosses/edl.json`

The `input/...` files they reference are not in the repo, so point them at your own recordings.

```jsonc
{
  "title": "...",
  "output": "exports/shorts/my-short/my-short.mp4",
  "encoder": "qsv",               // or "x264" without an Intel GPU
  "fps": 30,                      // optional; default = source fps (GTA 6 is 30 fps)
  "segments": [
    {"src": "input/gta6_x.webm", "in": 120.5, "out": 141.0,
     "texts": [
       {"lines": ["COPS CAN *DESCRIBE* YOU?"], "style": "hook",    "start": 0.0, "end": 2.5, "y": 300, "size": 130},
       {"lines": ["GTA 6"],                    "style": "label",   "start": 0.0, "end": 20,  "y": 470, "size": 46},
       {"lines": ["SO I CHANGED MY *OUTFIT*"],  "style": "caption", "start": 3.0, "end": 12,  "y": 1310, "size": 80}
     ]}
  ]
}
```

Long-form only:
- `{"type": "card", "dur": 3, "lines": ["LABEL", "BIG *TITLE*"], "src": ..., "t": ...}` adds a title card.
- `"chapter": "Name"` on a segment starts a chapter.
- `"narration": {"voice": "am_michael", "lines": [{"at": 1.0, "text": "..."}]}` adds narration (Kokoro).
- For ElevenLabs, use:
  `"narration": {"engine": "elevenlabs", "voice": "Adam", "elevenlabs": {"model": "eleven_multilingual_v2", "stability": 0.35, "style": 0.45}, "lines": [...]}`
  - `model`: `eleven_multilingual_v2` (natural, stable) or `eleven_v3` (most expressive; supports audio tags in the text such as `[excited]` or `[whispers]`; check ElevenLabs' docs for the current tag list).
  - Lower `stability` gives more emotional range. Higher `style` gives a more exaggerated delivery.
  - `speed` 0.7–1.2.

**Narration cost tips:** write the final script first, then render. Changing a line's text, voice or settings creates a new paid generation; identical lines come from the cache. The credit balance is on your ElevenLabs dashboard.

**Caption placement:**
- Shorts: keep text between y=280 and y=1500. YouTube's UI covers the bottom ~350 px and the right edge.
- Long-form: put captions at the top (y 40–100), clear of game HUDs and boss bars.

## 7. Extending it

- **Tuning detection:** every threshold is in `config/default.json`. Change one value at a time and measure with `evaluate.py` against labelled recordings.
- **Adding a game profile** (the best way to add events like "WASTED" or "MISSION PASSED" in GTA 6):
  ```powershell
  py scripts\make_template.py frame input\gta6_x.webm --t 12:34          # frame with a 10% grid -> find the banner
  py scripts\make_template.py crop  input\gta6_x.webm --t 12:34 --also-t 25:10 --roi 0.30 0.40 0.40 0.15 --profile gta6 --id wasted --event death
  py scripts\detect.py input\gta6_other.webm --profile gta6
  ```
  `--also-t` (another time the same banner appears) gives a much better mask. Combine events with `"encounters"` rules in `profile.json` (see `profiles/_example/profile.example.json`). Build templates from your own footage, never guessed positions. Test on a recording you did not build the template from.
- **New caption style:** add an entry to `STYLES` in `scripts/captions.py`.
- **New layout** (for example a zoomed 9:16 crop): `segment_graph()` in `render_short.py`.
- **NVIDIA GPU encoding:** add an `nvenc` branch to `venc()` in `render_short.py` (`-c:v h264_nvenc`).
- **Another voice engine:** `tts.py` routes everything through `engine(name)` and `speak(k, text, voice, speed)`. Add a class like `ElevenLabs` with a `speak()` returning `(float32 mono samples, sample_rate)`, and add a branch in `engine()`.
- Run the tests after every change: `py -m unittest discover -s tests`.

## 8. Things to know

- **HDR:** PS5 WebM captures with HDR on are 10-bit, 1920x**1088**. Tone mapping and padding removal are automatic. If colours look dull, record with HDR off.
- **Decoding speed:** `--hwaccel auto` (the default) uses the GPU for 10-bit HDR files and the CPU for 8-bit H.264, as measured on real PS5 files.
- **Kokoro** is clear but calm. Use it for cinematic or calm videos and drafts.
- **ElevenLabs** is for hype-style narration. Pick ONE voice for the channel and keep it. Label AI voice on TikTok.
- Whichever engine you use, punchy scripts matter more than the voice (see `voice_test.py`).
- **Known gotcha:** OpenCV 5's `phaseCorrelate` modifies its inputs in place. `detect.py` passes copies. Keep it that way.

## 9. Content and rights rules (GTA 6)

From `docs/GTA6_Shorts_Launch_Plan.pdf`. Check it for the formats, hooks and posting plan.
- No cutscenes, endings, story dialogue or mission reveals. Rockstar takes those down.
- No graphic violence in the first 7 seconds or on the thumbnail (YouTube ad rules).
- Licensed radio music off. No other creators' footage. Only royalty-free music.
- Captions and narration describe only what is actually on screen.
- Label AI voices on TikTok.
- A person makes the creative decisions (hooks, scripts, picks). YouTube demonetizes fully automated, mass-produced channels.
- Rockstar's policy on **monetized** videos is not clearly confirmed. Check it before monetizing.
