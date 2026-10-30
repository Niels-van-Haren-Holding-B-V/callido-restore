# Handovers

One file per handover, named `YYYY-MM-DD-HHMM-<author>.md` (e.g. `2026-10-30-2140-grok.md`, `2026-10-30-2200-claude.md`).
Keep it short: **Done** (what changed, commits), **Found** (facts, measurements, failures with exact messages), **Next** (what the
other side should do), **Blocked** (what you need). Claude pulls and reads this folder at the start of every work session;
Grok does the same. Code changes go in commits/PRs, not in handovers.
