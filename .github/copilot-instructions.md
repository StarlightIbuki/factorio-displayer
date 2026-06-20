# factorio-display Copilot Instructions

This project builds Factorio blueprints for audio/video display using combinators and programmable speakers.

## Dev Notes

- In this workspace's PowerShell flow, avoid chaining with `&&`; use `Set-Location <path>; <command>` instead.
- Python runs from `.venv/Scripts/python.exe` under the repo root.
- In apply_patch for Python files, prefer function-scoped `@@ def ...` context; broad context can misplace edits.
- Run tests: `.venv\Scripts\python.exe -m pytest tests/ -v --tb=short`
- Draftsman warnings (e.g. UnknownNoteWarning) are non-fatal; blueprints still generate correctly.
- `blueprint.entities` after `Blueprint.from_string()` gives parsed entity objects with `.tile_position`, `.name`, `.volume_signal`.
- Sub-tick values in CC entries must match the modulo AC output (0-based or 1-based consistently).

## Rules

Before implementation:
1. Confirm design with me by rephrasing in your own words
1. Make sure the plan is feasible and sound
1. Write unit tests
1. Add proper debug logs that benefit debugging
1. Think! Think if anything is odd and ask! Do not proceed until you are crystal clear on the design and implementation plan.

When in trouble, including multiple times of failure, assumption broken, reflect:
1. What's the original purpose? Does it make sense?
1. Debug with logs, and add more logs if needed.
1. And when fail to come up with a reasonable answer, ask me for help.

When a non-trivial iteration finishes, before claiming a feature/bug is done:
1. Always run tests

When fixed a bug:
1. If not covered by tests, add a test to cover it

## Architecture Reference

Before making any changes, read **`architecture.md`** for the full architecture: audio pipeline , combinator conventions, wiring, entity layout coordinates, and test structure.
