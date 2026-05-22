"""Shared LaTeX helpers for marketplace comparison tables."""

from __future__ import annotations

from pathlib import Path

MARKETPLACE_ORDER = [
    ("ClawHub", "ClawHub"),
    ("skillsdirectory", "SkillsDir."),
    ("skills_sh", "Skills.sh"),
    ("gharchive", "GitHub"),
]


def _pct(value: float) -> str:
    return f"{100.0 * value:.1f}\\%"


def write_marketplace_latex_table(
    out_path: str,
    *,
    row_header: str,
    row_names: list[str],
    skills_ratios: dict[tuple[str, str], float],
    findings_ratios: dict[tuple[str, str], float],
    caption: str,
    label: str,
) -> None:
    """Write a compact table with skill/finding percentages per marketplace."""

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = " & ".join(f"\\textbf{{{display}}}" for _key, display in MARKETPLACE_ORDER)
    lines = [
        "\\begin{table}[t]",
        "\\setlength{\\tabcolsep}{3pt}",
        f"\\caption{{{caption}}}",
        f"\\label{{{label}}}",
        "\\centering",
        "\\begin{tabularx}{\\linewidth}{Xrrrr}",
        "\\toprule",
        f"\\textbf{{{row_header}}} & {header} \\\\",
        "\\midrule",
    ]
    for row_name in row_names:
        cells = []
        for market, _display in MARKETPLACE_ORDER:
            skill_ratio = skills_ratios.get((row_name, market), 0.0)
            finding_ratio = findings_ratios.get((row_name, market), 0.0)
            cells.append(f"{_pct(skill_ratio)} / {_pct(finding_ratio)}")
        lines.append(f"{row_name} & " + " & ".join(cells) + " \\\\")
    lines.extend(["\\bottomrule", "\\end{tabularx}", "\\end{table}"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
