"""Generation du rapport Markdown."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from betanalyst.models import Analysis, MatchBundle

DISCLAIMER = (
    "> Rapport genere automatiquement. Aucun modele, statistique ou LLM, ne predit "
    "un resultat sportif de maniere fiable. A utiliser comme aide a la decision "
    "uniquement, jamais comme garantie. Jouez de maniere responsable."
)


def _probability_table(bundle: MatchBundle) -> str:
    forebet, poisson = bundle.forebet, bundle.poisson
    implied = forebet.implied_probabilities() if forebet else None
    rows = [
        "| Source | 1 | X | 2 | Over 2.5 | BTTS |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    def fmt(value: float | None) -> str:
        return f"{value:.1f} %" if value is not None else "-"

    has_forebet_probabilities = forebet and any(
        value is not None for value in (forebet.prob_home, forebet.prob_draw, forebet.prob_away)
    )
    if has_forebet_probabilities:
        rows.append(
            f"| Forebet | {fmt(forebet.prob_home)} | {fmt(forebet.prob_draw)} | "
            f"{fmt(forebet.prob_away)} | - | - |"
        )
    if implied:
        rows.append(
            f"| Cotes (implicite) | {fmt(implied.get('1'))} | {fmt(implied.get('X'))} | "
            f"{fmt(implied.get('2'))} | - | - |"
        )
    if poisson:
        rows.append(
            f"| Poisson | {fmt(poisson.prob_home)} | {fmt(poisson.prob_draw)} | "
            f"{fmt(poisson.prob_away)} | {fmt(poisson.prob_over_25)} | {fmt(poisson.prob_btts)} |"
        )
    return "\n".join(rows) if len(rows) > 2 else "_Aucune probabilite disponible._"


def _form_block(bundle: MatchBundle) -> str:
    stats = bundle.stats
    lines = []
    for form in (stats.home_form, stats.away_form):
        if not form:
            continue
        lines.append(
            f"- **{form.name}** : forme {''.join(form.last_results) or '?'} | "
            f"{form.avg_goals_for:.2f} bm/match | {form.avg_goals_against:.2f} be/match | "
            f"{form.points_per_game:.2f} pts/match"
        )
    if stats.head_to_head:
        lines.append("- **Confrontations directes** :")
        lines.extend(f"  - {line}" for line in stats.head_to_head)
    return "\n".join(lines) if lines else "_Statistiques Flashscore indisponibles._"


def build_markdown(pairs: list[tuple[MatchBundle, Analysis | None]]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [f"# Rapport d'analyse - {now}", "", DISCLAIMER, ""]

    for bundle, analysis in pairs:
        parts.append(f"## {bundle.label}")
        meta = " | ".join(filter(None, [bundle.stats.competition, bundle.stats.kickoff]))
        if meta:
            parts.append(f"_{meta}_")
        parts += ["", "### Donnees", _form_block(bundle), "", _probability_table(bundle), ""]
        if analysis:
            parts += [f"### Analyse LLM ({analysis.model})", analysis.markdown, ""]
        else:
            parts += ["### Analyse LLM", "_Non generee (LLM desactive ou injoignable)._", ""]
        parts.append("---")

    return "\n".join(parts)


def write_report(
    pairs: list[tuple[MatchBundle, Analysis | None]], output_dir: Path
) -> tuple[Path, Path]:
    """Ecrit le rapport Markdown et le JSON brut. Retourne les deux chemins."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    markdown_path = output_dir / f"rapport-{stamp}.md"
    markdown_path.write_text(build_markdown(pairs), encoding="utf-8")

    json_path = output_dir / f"donnees-{stamp}.json"
    json_path.write_text(
        json.dumps(
            [
                {"data": bundle.to_dict(), "analysis": analysis.markdown if analysis else None}
                for bundle, analysis in pairs
            ],
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return markdown_path, json_path
