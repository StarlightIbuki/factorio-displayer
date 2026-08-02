# Roadmap

Backlog of planned work, tracked here until implemented.  Implemented items
live in `docs/architecture.md` / the codebase; this file is for what's next.

## ASCII-art blueprint renderer — DONE (CLI + web)

**CLI:** `factorio-display blueprint-ascii <file|-> [-o out] [--no-coords]`
renders a blueprint as ASCII art — a glyph/entity map plus per-colour
circuit-wiring maps.  Implementation: `src/factorio_display/ascii_render.py`.
Tests: `tests/test_ascii_render.py`.

**Web:** exposed in the web app (backend renders, so it's the exact same
output as the CLI — no JS port drift):

- Backend endpoint `POST /api/v1/blueprints/ascii` (auth-gated like the
  decode endpoint) reuses `ascii_render.blueprint_string_to_ascii`.
- **Blueprint viewer utility** ("Blueprint viewer" → paste → **ASCII art**
  button) renders the entity + wiring maps into a scrollable `<pre>`.
- **Job result panel** gains a **"Debug ASCII"** tab that renders the job's
  blueprint the same way.
- `api/static/ascii.js` (thin client) + `pre.ascii` styling; i18n keys added
  (en + zh-CN).  Test: `test_blueprint_ascii_endpoint`.

## Backlog notes

- **Wire-length diagnostics**: a follow-up to the ASCII renderer could
  highlight cells whose bridge wire exceeds Factorio's ~9-tile circuit reach
  (the single-frame memory→display bug).  The wiring maps already make the
  symptom obvious; an explicit `!` marker on over-long connections would make
  it unmissable.
- Optional "diff" mode comparing two jobs' wiring maps (e.g. single-frame vs
  multi-frame) to spot missing/long wires at a glance.
