"""Eval report: evals/reports/<date>-<prompt_version>.md + .json (SPEC §1.7.8)."""

import json
from pathlib import Path

from leadradar_ai.evals.runner import EvalResult

PRECISION_TARGET = 0.8


def _fmt(x: float | None) -> str:
    return "—" if x is None else f"{x:.3f}"


def _table(rows: dict[str, dict]) -> list[str]:
    lines = ["| | TP | FP | FN | TN | Precision | Recall | F1 |", "|---|---|---|---|---|---|---|---|"]
    for name, m in rows.items():
        lines.append(
            f"| {name} | {m['tp']} | {m['fp']} | {m['fn']} | {m['tn']} | {_fmt(m['precision'])} | "
            f"{_fmt(m['recall'])} | {_fmt(m['f1'])} |"
        )
    return lines


def render_markdown(result: EvalResult) -> str:
    m = result.metrics
    precision = m.overall["precision"]
    verdict = (
        "no positive predictions yet"
        if precision is None
        else (
            f"{'✅ meets' if precision >= PRECISION_TARGET else '❌ below'} the target of {PRECISION_TARGET}"
        )
    )
    lines = [
        f"# Eval report — {result.prompt_version}",
        "",
        f"- Date: {result.now.date()} · mode: {result.mode}",
        f"- Decisions: {len(result.decisions)} (evaluated {m.evaluated}, not evaluated {m.not_evaluated})",
        f'- **Precision on "yes": {_fmt(precision)}** — {verdict}',
        f"- Recall: {_fmt(m.overall['recall'])} · F1: {_fmt(m.overall['f1'])}",
        (
            f'- Abstention ("unclear"): {_fmt(m.abstention_rate)} · '
            f"hallucinated quotes: {_fmt(m.hallucination_rate)} of {m.evidence_total} evidence items"
        ),
        f"- LLM calls: {m.llm_calls} ({_fmt(m.llm_calls_per_company)} per company)",
        "",
        "## Overall",
        *_table({"all": m.overall}),
        "",
        "## By category",
        *_table(m.by_category),
        "",
        "## By service",
        *_table(m.by_service),
        "",
        "## Verifier",
        "",
        ", ".join(f"{k}: {v}" for k, v in m.rejected_by_reason.items()) or "nothing rejected",
    ]

    fps = [d for d in result.decisions if d.outcome == "FP"]
    lines += ["", f"## False positives ({len(fps)})", ""]
    for d in fps:
        lines.append(f"- **{d.company_domain} · {d.question_key}** — expected no, verified yes")
        lines += [f"  - “{q}”" for q in d.quotes]

    fns = [d for d in result.decisions if d.outcome == "FN"]
    lines += ["", f"## False negatives ({len(fns)})", ""]
    for d in fns:
        why = (
            "no candidate snippets (data or prefilter)" if d.auto_no else f"model/verifier said {d.predicted}"
        )
        rejected = f"; rejected: {d.rejected}" if d.rejected else ""
        hint = f"; hint: {d.evidence_hint}" if d.evidence_hint else ""
        lines.append(f"- **{d.company_domain} · {d.question_key}** — {why}{rejected}{hint}")

    if result.evidence:
        ev = result.evidence_summary
        lines += [
            "",
            "## Labelled evidence and traps",
            "",
            f"- Supporting evidence verified: {ev['accept_verified']} of {ev['accept']}",
            f"- Traps that became a signal: {ev['traps_leaked']} of {ev['traps']}",
        ]
        leaked = [e for e in result.evidence if e.expected == "reject" and e.signal]
        lines += [f"  - **{e.company_domain} · {e.question_key}** ({e.reason}): “{e.quote}”" for e in leaked]

    if result.scores:
        lines += [
            "",
            "## Scores",
            "",
            "| Company | Service | Priority | Tier | Rules |",
            "|---|---|---|---|---|",
        ]
        for sc in result.scores:
            tier = f"{sc.tier} (outside ICP)" if sc.outside_icp else sc.tier
            lines.append(
                f"| {sc.company_domain} | {sc.service} | {sc.priority} | {tier} | {', '.join(sc.rules) or '—'} |"
            )

    if result.notes:
        lines += ["", "## Notes", "", *[f"- {n}" for n in result.notes]]
    return "\n".join(lines) + "\n"


def write_report(result: EvalResult, out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{result.now.date()}-{result.prompt_version.replace('@', '-')}"
    md, js = out / f"{stem}.md", out / f"{stem}.json"
    md.write_text(render_markdown(result), encoding="utf-8")
    payload = result.model_dump(mode="json")
    payload["decisions"] = [d.model_dump(mode="json") | {"outcome": d.outcome} for d in result.decisions]
    payload["evidence_summary"] = result.evidence_summary
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return md, js
