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
1. confirm design with me by repharsing in your own words
1. make sure the plan is feasible and sound
1. write unit tests

When iterating/writting code, always stop and ask for help from me if:
1. you tried a solution and it does not work;
1. something you are not sure about;
1. any assumption is broken

When a non-trivial iteration finishes, before claiming a feature/bug is done:
1. always run tests

When fixed a bug:
1. if not covered by tests, add a test to cover it

## Architecture Reference

Before making any changes, read **`architecture.md`** for the full architecture: audio pipeline (pitch_mapping → encoder → player_blueprint), combinator conventions (AC/DC/CC/SPK wiring), wire colors (red=page data bus, green=CC lookup/bell bus), entity layout coordinates, and test structure.
