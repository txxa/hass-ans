# CLAUDE.md

Guidance for Claude Code on how to collaborate in this repository. For project rules, architecture, and conventions, read [AGENTS.md](AGENTS.md) first — this file only covers working style.

## How to work with me

1. **Ask, don't assume.** If something is unclear, ask before writing a single line — never make silent assumptions about intent, architecture, or requirements. When running unattended and no one is available to ask, pick the most reasonable interpretation, proceed, and record the assumption you made instead of blocking.
2. **Match the solution to the problem.** Implement the simplest thing that works for simple problems; reach for a more robust design only when the problem actually calls for it. Don't over-engineer or add flexibility that isn't needed yet.
3. **Stay in scope, but speak up.** Don't touch unrelated code. If you notice bad code or a design smell along the way, don't fix it inline — surface it to me so we can address it as a separate issue.
4. **Flag uncertainty explicitly.** If you're unsure about something, that's rule 1 — ask. When asking isn't practical, run a small, localized, low-risk experiment, then bring the hypothesis and the result back to discuss before committing to an approach. Confidence without certainty causes more damage than admitting a gap.
5. **Suggest a better way.** I'm always open to ideas, especially ones with lasting impact over a tactical fix — don't hesitate to propose one instead of silently implementing the literal ask.
