"""Aggregate all evaluation reports into a readable markdown summary."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def note_summary(n: dict) -> str:
    return (f"cov={_fmt(n['coverage'])} seg={_fmt(n['segmentation_recall'])} "
            f"rhythm={_fmt(n['rhythm_recall'])} exact={_fmt(n['exact_acc'])} "
            f"onset={_fmt(n['onset_err'])} dur={_fmt(n['dur_err'])} "
            f"vel={_fmt(n['vel_corr'])} ({n['n_matched']}/{n['n_ref']} notes)")


def audio_summary(a: dict) -> str:
    return (f"chroma={_fmt(a['chroma_cos'])} dtw={_fmt(a['chroma_dtw'])} "
            f"env={_fmt(a['env_corr'])} spec={_fmt(a['spec_cos'])}")


def main() -> None:
    lines: list[str] = []
    lines.append("# MIDI Translator Evaluation — Summary\n")

    # ── core fidelity (auto rail mode) ─────────────────────────────
    auto = json.loads((ROOT / "eval_midi/out/auto/report.json").read_text(encoding="utf-8"))
    lines.append("## Core fidelity: MIDI -> Factorio (rail_mode=auto, map_drums=off)\n")
    lines.append("| scenario | rails | coverage | segmentation | rhythm | exact | onset | dur | vel | chroma_cos* | spec_cos* | env_corr* |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in auto:
        n = r["note"]
        a = r["audio_content"]
        lines.append(
            f"| {r['name']} | {','.join(r['instruments'])} | {_fmt(n['coverage'])} | "
            f"{_fmt(n['segmentation_recall'])} | {_fmt(n['rhythm_recall'])} | "
            f"{_fmt(n['exact_acc'])} | {_fmt(n['onset_err'])} | {_fmt(n['dur_err'])} | "
            f"{_fmt(n['vel_corr'])} | {_fmt(a['chroma_cos'])} | {_fmt(a['spec_cos'])} | "
            f"{_fmt(a['env_corr'])} |"
        )
    lines.append("\n* = timbre-free 'content' audio comparison (original vs reconstructed notes, same piano synth)\n")

    lines.append("\n### Factorio in-game emulation (original vs speaker render)\n")
    lines.append("| scenario | chroma_cos | dtw | env_corr | spec_cos |")
    lines.append("|---|---|---|---|---|")
    for r in auto:
        a = r["audio_factorio"]
        lines.append(f"| {r['name']} | {_fmt(a['chroma_cos'])} | {_fmt(a['chroma_dtw'])} | "
                     f"{_fmt(a['env_corr'])} | {_fmt(a['spec_cos'])} |")
    lines.append("")

    # ── map_drums variant ──────────────────────────────────────────
    md = json.loads((ROOT / "eval_midi/out/mapdrums/report.json").read_text(encoding="utf-8"))
    lines.append("\n## Effect of `--map-drums` (below-range notes -> kick drum)\n")
    lines.append("| scenario | rails | coverage | exact | note_count(ref/rec) |")
    lines.append("|---|---|---|---|---|")
    for r in md:
        n = r["note"]
        lines.append(f"| {r['name']} | {','.join(r['instruments'])} | {_fmt(n['coverage'])} | "
                     f"{_fmt(n['exact_acc'])} | {n['n_ref']}/{n['n_rec']} |")
    lines.append("")

    # ── AI chain ───────────────────────────────────────────────────
    bp = json.loads((ROOT / "eval_midi/out/bp/report.json").read_text(encoding="utf-8"))
    lines.append("\n## AI transcription (Basic Pitch) end-to-end\n")
    lines.append("| chain | stage | coverage | seg | rhythm | exact | onset | precision | chroma_cos | env_corr | spec_cos |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in bp:
        name = r["name"]
        if "transcription" in r:
            n = r["transcription"]
            a = r.get("audio_factorio", {})
            lines.append(
                f"| {name} | AI transcribe | {_fmt(n.get('coverage',0))} | {_fmt(n.get('segmentation_recall',0))} | "
                f"{_fmt(n.get('rhythm_recall',0))} | {_fmt(n.get('exact_acc',0))} | "
                f"{_fmt(n.get('onset_err',0))} | {_fmt(n.get('precision',0))} | "
                f"{_fmt(a.get('chroma_cos',0))} | {_fmt(a.get('env_corr',0))} | {_fmt(a.get('spec_cos',0))} |"
            )
        if "final" in r:
            n = r["final"]
            lines.append(
                f"| {name} | +Factorio | {_fmt(n.get('coverage',0))} | {_fmt(n.get('segmentation_recall',0))} | "
                f"{_fmt(n.get('rhythm_recall',0))} | {_fmt(n.get('exact_acc',0))} | "
                f"{_fmt(n.get('onset_err',0))} | {_fmt(n.get('precision',0))} | - | - | - |"
            )
    lines.append("")

    report = "\n".join(lines)
    out = ROOT / "eval_midi" / "SUMMARY.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n(written to {out})")


if __name__ == "__main__":
    main()
