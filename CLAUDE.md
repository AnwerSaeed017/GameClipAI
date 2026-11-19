# GameClip AI: notes for Claude Code

Read these before changing code or making videos:
- [docs/PROJECT_CONTEXT.md](docs/PROJECT_CONTEXT.md): goals, architecture, design decisions, findings from real footage, bugs already fixed, what's validated and what's next.
- [docs/SETUP.md](docs/SETUP.md): install, commands, edit-plan (EDL) format, how to extend.
- [docs/CLAUDE_LONG_VIDEO.md](docs/CLAUDE_LONG_VIDEO.md): how to make a long video (structure, script voice, EDL, review checklist).
- [docs/CLAUDE_SHORT_VIDEO.md](docs/CLAUDE_SHORT_VIDEO.md): how to make a Short (layout, subtitles, script, review).
- `.claude/skills/gameclip/SKILL.md` (`/gameclip`): the session workflow. Ingest → detect → whole-session review → proposals → user yes/no → render.
- `docs/GTA6_Shorts_Launch_Plan.pdf` (local only, not in git): GTA 6 content formats, hooks and platform rules.

Hard rules:
- Never caption or narrate events that aren't visible on screen.
- Never commit media, models, `input/`, `analysis/` or secrets. Never print or read out the ElevenLabs key in `.env`.
- ElevenLabs narration costs credits: confirm the final script with the user before generating.
- Run `.venv\Scripts\python.exe -m unittest discover -s tests` after code changes.
