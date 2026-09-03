"""Generation du rapport Markdown."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from betbot import consensus
from betbot.combo import (
    MAX_LEG_VALUE,
    MIN_LEG_ODDS,
    MIN_LEG_PROBABILITY,
    VALUE_TICKET_SIZES,
    Ticket,
    build_max_ticket,
    build_value_ticket,
    kelly_share,
    market_calibrated,
)
from betbot.models import Analysis, MatchBundle, implied_from_odds
from betbot.poisson import SOURCE_FORM, SOURCE_FORM_ONLY
from betbot.trap import TRAP_MARKETS, predicted_closed_game, trap_reasons

DISCLAIMER = (
    "> Rapport genere automatiquement a partir de Forebet, Flashscore, d'un modele de "
    "Poisson et des cotes Unibet. Ce sont des **pronostics**, jamais une validation : "
    "aucun modele, statistique ou LLM, ne predit un resultat sportif de maniere fiable. "
    "Le pari sportif est un jeu d'argent, perdant a long terme du fait de la marge du "
    "bookmaker. Ne mise que ce que tu peux perdre. Aide : 09 74 75 13 13 "
    "(Joueurs Info Service). Interdit aux mineurs."
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
            f"| Poisson ({poisson.source}) | {fmt(poisson.prob_home)} | {fmt(poisson.prob_draw)} | "
            f"{fmt(poisson.prob_away)} | {fmt(poisson.prob_over_25)} | {fmt(poisson.prob_btts)} |"
        )
        rows += ["", _model_note(bundle)]
    return "\n".join(rows) if len(rows) > 2 else "_Aucune probabilite disponible._"


def _model_note(bundle: MatchBundle) -> str:
    """Origine des buts attendus et fiabilite associee."""
    poisson = bundle.poisson
    if not poisson:
        return ""
    goals = (
        f"Buts attendus : {poisson.expected_home_goals:.2f} - "
        f"{poisson.expected_away_goals:.2f} (score le plus probable {poisson.most_likely_score})."
    )
    if poisson.source == SOURCE_FORM:
        return (
            f"_{goals} **Aucune cote pour caler le modele** : l'estimation ne repose que "
            "sur la forme recente, sans le contre-poids du marche. A traiter comme un "
            "ordre de grandeur, pas comme une probabilite._"
        )
    gap = poisson.calibration_gap
    if poisson.source == SOURCE_FORM_ONLY:
        ecart = (
            f" Ecart maximal avec le marche : {gap:.1f} pts." if gap is not None else ""
        )
        return (
            f"_{goals} Estimation tiree des vingt derniers matchs de chaque equipe (les "
            "recents plus lourds, domicile et exterieur distingues, niveau des adversaires "
            f"retire, saison en ancrage), sans reference aux cotes.{ecart} Au-dela "
            "d'une dizaine de points, l'ecart mesure d'abord l'incertitude du modele, pas "
            "une occasion (`--poisson marche` pour l'estimation calee sur les cotes)._"
        )
    calibration = f" Ecart maximal aux marches cotes : {gap:.1f} pts." if gap is not None else ""
    return (
        f"_{goals} Modele cale sur les cotes ({poisson.source}), marge du bookmaker "
        f"retiree.{calibration} Un ecart avec le marche vient de la forme recente, "
        "volontairement bornee : le marche en sait davantage._"
    )


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


def _markets_block(bundle: MatchBundle, *, top: int = 12) -> str:
    """Marches classes par probabilite, avec la cote minimale a exiger.

    Le modele calcule plus de cent marches, dont beaucoup d'evidences invendables
    (« plus de 0.5 but ») : seuls ceux reellement cotes sont affiches quand il y en a.
    """
    if not bundle.poisson or not bundle.poisson.markets:
        return ""

    available = bundle.best_odds()
    rows = [
        "| Marche | Proba modele | Cote equitable | Cote dispo | Valeur |",
        "| --- | --- | --- | --- | --- |",
    ]
    priced = {
        market: probability
        for market, probability in bundle.poisson.markets.items()
        if available.get(market)
    }
    ranked = sorted((priced or bundle.poisson.markets).items(), key=lambda i: i[1], reverse=True)
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


def _forebet_markets_block(bundle: MatchBundle) -> str:
    """Probabilites des pages Forebet specialisees, face au modele et a la cote.

    Les deux estimations brutes restent affichees a cote de leur consensus : masquer
    l'ecart derriere une moyenne donnerait une assurance que ni l'une ni l'autre n'a.
    Un desaccord marque est signale, et suffit a ecarter le marche des combines.
    """
    markets = bundle.forebet.markets if bundle.forebet else {}
    if not markets:
        return ""

    available = bundle.best_odds()
    detail = consensus.summary(bundle)
    rows = [
        "| Marche | Proba Forebet | Proba modele | Cote implicite | Retenu | Confiance | Cote |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for market, probability in sorted(markets.items(), key=lambda item: item[1], reverse=True):
        line = detail.get(market, {})
        mine, implied = line.get("modele"), line.get("cote_implicite")
        kept, gap, confidence = line.get("retenu"), line.get("ecart"), line.get("confiance")
        if kept is None:
            retained = f"ecarte ({gap:.0f} pts d'ecart)" if isinstance(gap, float) else "-"
        elif line.get("desaccord"):
            retained = f"{kept:.1f} % (cote arbitre)"
        else:
            retained = f"{kept:.1f} %"
        rows.append(
            f"| {market} | {probability:.0f} % | "
            f"{f'{mine:.1f} %' if isinstance(mine, float) else '-'} | "
            f"{f'{implied:.1f} %' if isinstance(implied, float) else '-'} | {retained} | "
            f"{f'{confidence:.0%}' if isinstance(confidence, float) else '-'} | "
            f"{f'{available[market]:.2f}' if available.get(market) else '-'} |"
        )
    rows += [
        "",
        "_La valeur retenue pese Forebet a 60 % et le modele a 40 %, puis tire vers la plus "
        "basse des deux a proportion de leur desaccord : la confiance affichee tombe a 0 a "
        f"la limite de tolerance (environ {consensus.MAX_DISAGREEMENT:.0f} points d'ecart, "
        "moins autour de 50 % ou l'ecart fait basculer la decision, plus aux extremes ou les "
        "deux sources disent la meme chose). Au-dela, la cote arbitre : la source la plus "
        "proche du marche l'emporte, sinon le marche est ecarte des combines. Les marches de "
        "mi-temps n'ont pas d'equivalent dans le modele, d'ou les colonnes vides. Deux "
        "estimations proches ne valident rien : elles peuvent se tromper ensemble._",
    ]
    return "\n".join(rows)


def _form_block(bundle: MatchBundle) -> str:
    stats = bundle.stats
    lines = []
    for form, table in ((stats.home_form, stats.home_table), (stats.away_form, stats.away_table)):
        if not form:
            continue
        rank = ""
        if table:
            conceded = table.conceded_per_game
            rank = (
                f" | {table.position}e du classement ({table.points} pts, "
                f"{conceded:.2f} be/match sur la saison)"
                if conceded is not None
                else f" | {table.position}e du classement ({table.points} pts)"
            )
        lines.append(
            f"- **{form.name}** : forme {''.join(form.last_results) or '?'} | "
            f"{form.avg_goals_for:.2f} bm/match | {form.avg_goals_against:.2f} be/match | "
            f"{form.points_per_game:.2f} pts/match{rank}"
        )
    if stats.head_to_head:
        lines.append("- **Confrontations directes** :")
        lines.extend(f"  - {line}" for line in stats.head_to_head)
    return "\n".join(lines) if lines else "_Statistiques Flashscore indisponibles._"


def _traps_block(bundle: MatchBundle) -> str:
    """Marches ecartes des combines parce que la rencontre les rend fragiles.

    Le modele et Forebet continuent d'afficher leurs probabilites : ce bloc dit seulement
    pourquoi Bet.Bot refuse de batir un combine dessus.
    """
    lines = []
    for market in TRAP_MARKETS:
        reasons = trap_reasons(bundle.stats, market, bundle.predicted_score)
        if reasons:
            lines.append(f"- **{market}** : {', '.join(reasons)}")
    if not lines:
        return ""
    if predicted_closed_game(bundle.predicted_score):
        lines.append(
            f"- **Rencontre annoncee fermee** : Forebet pronostique "
            f"{bundle.predicted_score} — un but decide le match."
        )
    return "\n".join(
        [
            "Marches ecartes des combines, la rencontre les rendant fragiles :",
            "",
            *lines,
            "",
            "_Un piege signale n'annonce pas l'issue contraire : il dit que la selection "
            "est moins sure que sa probabilite ne le laisse croire._",
        ]
    )


def _selection_block(bundles: list[MatchBundle]) -> str:
    """Recapitulatif en tete de rapport : le meilleur marche cote de chaque match."""
    rows = [
        "| Match | Marche | Cote | Proba modele | Valeur | Mise conseillee |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    found = positive = False
    calibrated = market_calibrated(bundles)
    for bundle in bundles:
        opportunities = bundle.opportunities()
        if not opportunities:
            continue
        # Une valeur enorme sur un gros outsider trahit presque toujours une faiblesse du
        # modele : on privilegie les selections plausibles, jouables telles quelles. Le
        # modele de forme s'ecartant du marche par construction, ses selections sont
        # classees par probabilite et le plafond de valeur ne s'y applique pas.
        playable = [
            item
            for item in opportunities
            if item[1] >= MIN_LEG_ODDS
            and item[2] >= MIN_LEG_PROBABILITY
            and (item[3] <= MAX_LEG_VALUE if calibrated else True)
        ]
        if not calibrated:
            playable.sort(key=lambda item: item[2], reverse=True)
        found = True
        market, odds, probability, value = (playable or opportunities)[0]
        share = kelly_share(probability, odds)
        positive = positive or share > 0
        stake = f"{100 * share:.1f} % du capital" if share else "ne pas jouer"
        rows.append(
            f"| {bundle.label} | {market} | {odds:.2f} | {probability:.1f} % | "
            f"{value:+.1f} % | {stake} |"
        )
    if not found:
        return ""
    rows += [
        "",
        "_Valeur = esperance de gain par euro mise si le modele a raison. La mise "
        "conseillee applique un quart du critere de Kelly, et vaut zero des que "
        "l'esperance est negative : la marge du bookmaker rend ce cas le plus frequent._",
    ]
    if not positive:
        rows.append(
            "\n**Aucune selection a esperance positive aujourd'hui.** Le modele etant cale "
            "sur les cotes, il ne trouve d'ecart que la ou la forme recente contredit le "
            "marche ; quand il n'en trouve aucun, s'abstenir est la decision qui rapporte "
            "le plus."
        )
    elif not calibrated:
        rows.append(
            "\n_Ces esperances sont celles du modele de forme, qui ne connait pas les "
            "cotes : elles sont larges parce qu'il est tranche, pas parce que le "
            "bookmaker s'est trompe. `--poisson marche` donne la lecture prudente._"
        )
    return "\n".join(rows)


def _ticket_block(ticket: Ticket, stake: float = 10.0) -> str:
    rows = [
        "| Coup d'envoi | Match | Marche | Proba | Source | Cote equitable | Cote dispo |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for leg in ticket.legs:
        odds = f"{leg.odds:.2f}" if leg.odds else "-"
        rows.append(
            f"| {leg.kickoff or '?'} | {leg.match} | {leg.market} | {leg.probability:.1f} % | "
            f"{leg.source} | {leg.fair_odds:.2f} | {odds} |"
        )

    deadline = ticket.deadline
    if deadline:
        rows += [
            "",
            f"**Pari a valider avant {deadline}**, coup d'envoi du premier match : "
            "passe cette heure, le combine n'est plus jouable tel quel.",
        ]

    rows += [
        "",
        f"**Probabilite que le ticket passe : {ticket.probability:.2f} %** "
        f"(environ une fois sur {ticket.one_in}).",
        "",
        f"Cote minimale a exiger pour que le pari ait un sens : **{ticket.fair_odds:.2f}**.",
    ]

    odds = ticket.odds
    rows.append("")
    if odds:
        payout = ticket.payout(stake)
        rows.append(
            f"Cote reellement disponible : **{odds:.2f}** "
            f"({stake:.0f} EUR rapportent {payout:.2f} EUR)."
        )
        value = ticket.value
        if value is not None:
            verdict = "esperance positive" if value > 0 else "esperance negative"
            rows.append(f"Valeur : **{value:+.1f} %** ({verdict} si le modele a raison).")
    else:
        rows.append(
            "_Cote du combine non calculable : au moins une selection n'est pas cotee "
            "chez Unibet (match hors des 50 prochaines rencontres, ou marche absent). "
            "Saisis les cotes manquantes via `--odds-csv`._"
        )

    rows += [
        "",
        "_Chaque selection ajoutee multiplie la probabilite par un nombre inferieur a 1 : "
        "le gain affiche grossit, la chance de le toucher s'effondre. Le calcul suppose "
        "de plus les matchs independants, ce qu'ils ne sont jamais totalement._",
    ]
    return "\n".join(rows)


def value_tickets(bundles: list[MatchBundle]) -> list[Ticket]:
    """Combines proposes en fin de rapport : les tailles fixes, puis le maximum.

    Le combine maximum n'est ajoute que s'il contient plus de selections que le plus
    long des tickets fixes : sinon il en serait la copie.
    """
    tickets = [build_value_ticket(bundles, legs=size) for size in VALUE_TICKET_SIZES]
    everything = build_max_ticket(bundles)
    if everything and len(everything.legs) not in VALUE_TICKET_SIZES:
        tickets.append(everything)
    return [ticket for ticket in tickets if ticket and ticket.legs]


def _ticket_to_dict(ticket: Ticket, stake: float = 10.0) -> dict:
    return {
        "titre": ticket.label or f"Combine {len(ticket.legs)} selections",
        "selections": [
            {
                "coup_denvoi": leg.kickoff,
                "match": leg.match,
                "marche": leg.market,
                "probabilite": leg.probability,
                "source_probabilite": leg.source,
                "cote_equitable": leg.fair_odds,
                "cote_disponible": leg.odds,
            }
            for leg in ticket.legs
        ],
        "a_valider_avant": ticket.deadline,
        "probabilite_estimee": ticket.probability,
        "une_fois_sur": ticket.one_in,
        "cote_equitable": ticket.fair_odds,
        "cote_disponible": ticket.odds,
        "valeur_theorique": ticket.value,
        "mise": stake,
        "gain_potentiel": ticket.payout(stake),
    }


def _value_tickets_block(bundles: list[MatchBundle]) -> list[str]:
    """Combines longs, marches melanges, classes du plus probable au plus gros gain."""
    parts: list[str] = []
    for ticket in value_tickets(bundles):
        parts += [
            f"## {ticket.label or f'Combine {len(ticket.legs)} selections (marches melanges)'}",
            _ticket_block(ticket),
            "",
            "---",
            "",
        ]
    return parts


def build_markdown(
    pairs: list[tuple[MatchBundle, Analysis | None]], ticket: Ticket | None = None
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [f"# Bet.Bot - rapport d'analyse - {now}", "", DISCLAIMER, ""]

    if ticket and ticket.legs:
        parts += [
            f"## Ticket combine ({len(ticket.legs)} selections)",
            _ticket_block(ticket),
            "",
            "---",
            "",
        ]

    parts += _value_tickets_block([bundle for bundle, _ in pairs])

    selection = _selection_block([bundle for bundle, _ in pairs])
    if selection:
        parts += ["## Selection", selection, "", "---", ""]

    for bundle, analysis in pairs:
        parts.append(f"## {bundle.label}")
        meta = " | ".join(
            filter(
                None,
                [bundle.stats.country, bundle.stats.competition, bundle.stats.kickoff],
            )
        )
        if meta:
            parts.append(f"_{meta}_")
        parts += ["", "### Donnees", _form_block(bundle), "", _probability_table(bundle), ""]
        odds_block = _odds_block(bundle)
        if odds_block:
            parts += ["### Cotes bookmakers", odds_block, ""]
        markets_block = _markets_block(bundle)
        if markets_block:
            parts += ["### Marches (modele Poisson)", markets_block, ""]
        forebet_block = _forebet_markets_block(bundle)
        if forebet_block:
            parts += ["### Marches (pages Forebet)", forebet_block, ""]
        traps_block = _traps_block(bundle)
        if traps_block:
            parts += ["### Matchs pieges", traps_block, ""]
        if analysis:
            parts += [f"### Analyse LLM ({analysis.model})", analysis.markdown, ""]
            verdict_block = _verdict_block(analysis)
            if verdict_block:
                parts += ["#### Reponses fermees", verdict_block, ""]
            if analysis.rebuttal:
                parts += ["#### Contre-analyse (avocat du diable)", analysis.rebuttal, ""]
        else:
            parts += ["### Analyse LLM", "_Non generee (LLM desactive ou injoignable)._", ""]
        parts.append("---")

    return "\n".join(parts)


def _verdict_block(analysis: Analysis) -> str:
    """Les quatre reponses fermees du LLM, puis le sort du verdict apres contre-analyse."""
    verdict = analysis.verdict
    if verdict is None:
        return ""
    rows = [
        ("Decision", verdict.decision),
        ("Marche retenu", verdict.market),
        ("Source la moins credible", verdict.least_credible),
        ("Risque principal", verdict.main_risk),
        ("Confiance /10", None if verdict.confidence is None else str(verdict.confidence)),
    ]
    if analysis.rebuttal:
        outcome = {True: "maintenu", False: "renverse", None: "non tranche"}[analysis.upheld]
        rows.append(("Verdict apres contre-analyse", outcome))
    lines = ["| Question | Reponse |", "| --- | --- |"]
    lines += [f"| {label} | {value or '_non repondu_'} |" for label, value in rows]
    return "\n".join(lines)


def write_report(
    pairs: list[tuple[MatchBundle, Analysis | None]],
    output_dir: Path,
    ticket: Ticket | None = None,
) -> tuple[Path, Path]:
    """Ecrit le rapport Markdown et le JSON brut. Retourne les deux chemins."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    markdown_path = output_dir / f"rapport-{stamp}.md"
    markdown_path.write_text(build_markdown(pairs, ticket), encoding="utf-8")

    bundles = [bundle for bundle, _ in pairs]
    tickets = ([ticket] if ticket and ticket.legs else []) + value_tickets(bundles)
    json_path = output_dir / f"donnees-{stamp}.json"
    json_path.write_text(
        json.dumps(
            {
                "matchs": [
                    {
                        "data": bundle.to_dict(),
                        "consensus": consensus.summary(bundle),
                        "analysis": analysis.markdown if analysis else None,
                        "verdict": (
                            analysis.verdict.to_dict()
                            if analysis and analysis.verdict
                            else None
                        ),
                        "rebuttal": analysis.rebuttal if analysis else None,
                        "upheld": analysis.upheld if analysis else None,
                    }
                    for bundle, analysis in pairs
                ],
                "combines": [_ticket_to_dict(item) for item in tickets],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return markdown_path, json_path
