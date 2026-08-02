# Roadmap

Backlog of planned work, tracked here until implemented.  Implemented items
live in `docs/architecture.md` / the codebase; this file is for what's next.

## ASCII-art blueprint renderer — frontend (planned)

**CLI (done):** `factorio-display blueprint-ascii <file|-> [-o out] [--no-coords]`
renders a blueprint as ASCII art for debugging — a glyph/entity map plus
per-colour circuit-wiring maps.  Implementation: `src/factorio_display/ascii_render.py`
(port target for the frontend).  Tests: `tests/test_ascii_render.py`.

**Frontend (todo):** expose the same view inside the web app so wiring bugs
can be inspected without the CLI.  Proposed UX:

- Reuse the existing **Blueprint viewer** ("View" on a job) and add a
  "Debug (ASCII)" tab/view next to the raw-string view.
- Implement a vanilla-JS port of `ascii_render.py` (`api/static/ascii.js`):
  parse the blueprint string with a minimal decoder (we already decode
  blueprint strings for Copy/View, so reuse that), then:
  1. **Entity map** — glyphs `D/A/S` (+facing `> < ^ V`), `C`, `S` speaker,
     `L` lamp, `.` unknown; tile coords header.
  2. **Wiring maps** — one grid per colour (RED / GREEN), each with its own
     `0-9 A-Z a-z` network-character pool (62 networks/map, extra maps when
     needed); per-entity cells show `{input}{output}` network chars.
  3. Render each grid as a `<pre>` block in a monospace panel with a legend
     listing each network char, its colour, and its member entities.
- Optional: a "diff" mode comparing two jobs' wiring maps (e.g. single-frame
  vs multi-frame) to spot missing/long wires at a glance.
- Mobile: not a priority (debug tool), but keep `<pre>` horizontally
  scrollable so phone users can at least pan the map.

Acceptance: opening a job's "Debug (ASCII)" view shows the same output the
CLI produces for that blueprint string (golden test against the CLI).

## Backlog notes

- **Wire-length diagnostics**: a follow-up to the ASCII renderer could
  highlight cells whose bridge wire exceeds Factorio's ~9-tile circuit reach
  (the single-frame memory→display bug).  The wiring maps already make the
  symptom obvious; an explicit `!` marker on over-long connections would make
  it unmissable.
