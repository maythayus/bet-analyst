"""Generation du rapport Markdown."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from betanalyst.models import Analysis, MatchBundle, implied_from_odds

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
    market = implied_from_odds(bundle.best_odds())
    if market:
        rows.append(
            f"| Marche (meilleure cote) | {fmt(market.get('1'))} | {fmt(market.get('X'))} | "
            f"{fmt(market.get('2'))} | - | - |"
        )
    if poisson:
        rows.append(
            f"| Poisson | {fmt(poisson.prob_home)} | {fmt(poisson.prob_draw)} | "
            f"{fmt(poisson.prob_away)} | {fmt(poisson.prob_over_25)} | {fmt(poisson.prob_btts)} |"
        )
    return "\n".join(rows) if len(rows) > 2 else "_Aucune probabilite disponible._"


def _odds_block(bundle: MatchBundle) -> str:
    if not bundle.bookmakers:
        return ""

    def cell(value: float | None) -> str:
        return f"{value:.2f}" if value else "-"

    rows = ["| Bookmaker | 1 | X | 2 |", "| --- | --- | --- | --- |"]
    for line in bundle.bookmakers:
        rows.append(
            f"| {line.bookmaker} | {cell(line.odds.get('1'))} | {cell(line.odds.get('X'))} | "
            f"{cell(line.odds.get('2'))} |"
        )

    gap = bundle.value_gap()
    if gap:
        best = max(gap.items(), key=lambda item: item[1])
        odds = bundle.best_odds().get(best[0])
        verdict = (
            f"Ecart Poisson - marche : 1 {gap['1']:+.1f} pts, X {gap['X']:+.1f} pts, "
            f"2 {gap['2']:+.1f} pts."
        )
        if best[1] > 0 and odds:
            verdict += (
                f" Seul signe ou le modele est plus optimiste que le marche : **{best[0]}** "
                f"(cote {odds:.2f}). A ne considerer que si les donnees sont completes."
            )
        else:
            verdict += " Aucun signe ou le modele bat le marche : pas de valeur detectee."
        rows += ["", verdict]
    return "\n".join(rows)


def _markets_block(bundle: MatchBundle, *, top: int = 8) -> str:
    """Marches classes par probabilite, avec la cote minimale a exiger."""
    if not bundle.poisson or not bundle.poisson.markets:
        return ""

    available = bundle.best_odds()
    rows = [
        "| Marche | Proba modele | Cote equitable | Cote dispo | Valeur |",
        "| --- | --- | --- | --- | --- |",
    ]
    ranked = sorted(bundle.poisson.markets.items(), key=lambda item: item[1], reverse=True)
    for market, probability in ranked[:top]:
        if probability <= 0:
            continue
        fair = 100 / probability
        offered = available.get(market)
        value = "-"
        if offered:
            value = f"{(offered * probability / 100 - 1) * 100:+.1f} %"
        rows.append(
            f"| {market} | {probability:.1f} % | {fair:.2f} | "
            f"{f'{offered:.2f}' if offered else '-'} | {value} |"
        )
    rows += [
        "",
        "_Cote equitable = cote en dessous de laquelle le pari perd de l'argent si le "
        "modele a raison. « Valeur » compare la cote proposee a cette cote equitable._",
    ]
    return "\n".join(rows)


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


def _selection_block(bundles: list[MatchBundle]) -> str:
    """Recapitulatif en tete de rapport : le meilleur marche cote de chaque match."""
    rows = [
        "| Match | Marche | Cote | Proba modele | Valeur |",
        "| --- | --- | --- | --- | --- |",
    ]
    found = False
    for bundle in bundles:
        opportunities = bundle.opportunities()
        if not opportunities:
            continue
        found = True
        market, odds, probability, value = opportunities[0]
        rows.append(
            f"| {bundle.label} | {market} | {odds:.2f} | {probability:.1f} % | {value:+.1f} % |"
        )
    if not found:
        return ""
    rows += [
        "",
        "_Valeur = esperance de gain par euro mise si le modele a raison. En dessous de "
        "+5 %, l'ecart est dans le bruit du modele ; au-dessus de +20 %, suspecte plutot "
        "une donnee manquante ou une equipe mal identifiee qu'une aubaine._",
    ]
    return "\n".join(rows)


def build_markdown(pairs: list[tuple[MatchBundle, Analysis | None]]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [f"# Rapport d'analyse - {now}", "", DISCLAIMER, ""]

    selection = _selection_block([bundle for bundle, _ in pairs])
    if selection:
        parts += ["## Selection", selection, "", "---", ""]

    for bundle, analysis in pairs:
        parts.append(f"## {bundle.label}")
        meta = " | ".join(filter(None, [bundle.stats.competition, bundle.stats.kickoff]))
        if meta:
            parts.append(f"_{meta}_")
        parts += ["", "### Donnees", _form_block(bundle), "", _probability_table(bundle), ""]
        odds_block = _odds_block(bundle)
        if odds_block:
            parts += ["### Cotes bookmakers", odds_block, ""]
        markets_block = _markets_block(bundle)
        if markets_block:
            parts += ["### Marches (modele Poisson)", markets_block, ""]
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
