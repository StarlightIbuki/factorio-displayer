# Roadmap

Backlog of planned work, tracked here until implemented.  Implemented items
live in `docs/architecture.md` / the codebase; this file is for what's next.

## Backlog notes

- **Wire-length diagnostics**: a follow-up to the ASCII renderer could
  highlight cells whose bridge wire exceeds Factorio's ~9-tile circuit reach
  (the single-frame memory→display bug).  The wiring maps already make the
  symptom obvious; an explicit `!` marker on over-long connections would make
  it unmissable.
- Optional "diff" mode comparing two jobs' wiring maps (e.g. single-frame vs
  multi-frame) to spot missing/long wires at a glance.
