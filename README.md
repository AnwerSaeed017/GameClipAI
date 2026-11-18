# GameClip AI

Local, free toolkit that turns PS5 gameplay recordings into YouTube Shorts and long-form videos.

- **Finds moments** in no-commentary footage. It uses game-audio spikes, on-screen action with camera movement filtered out, static/menu detection, and optional per-game HUD templates. It writes contact sheets and whole-session timelines for review.
- **Renders Shorts** (1080x1920) and **long-form** (1920x1080) videos, with animated captions, keyword highlights, chapters, HDR→SDR conversion, Intel Quick Sync encoding and YouTube loudness.
- **Narrates** with Kokoro (local, free for commercial use) or **ElevenLabs** (expressive; key in `.env`, lines cached so credits aren't spent twice). Game audio dips automatically under the voice.
- Works with **Claude Code**: the `/gameclip` skill runs the session workflow (review, propose clips, approve, render).

**Start here: [docs/SETUP.md](docs/SETUP.md)** covers install, workflow, edit-plan format and how to extend it.
**Background:** [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md) covers design decisions, findings from real footage, what's validated and the roadmap.

Status: the detector, both renderers and the narration engines are tested (25 tests: synthetic video, mocked ElevenLabs API) and used on real PS5 clips. Detection accuracy on long, uncut gameplay sessions has not been measured yet.
