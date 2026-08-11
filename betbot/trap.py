"""Detection des matchs pieges : rencontres ou la selection la plus probable sur le
papier est la plus fragile en pratique.

Trois lectures se completent, toutes tirees de Flashscore :

- les **confrontations directes**, qui gardent la memoire de rencontres fermees ou
  prolifiques que la forme du moment ne raconte pas ;
- le **classement**, qui mesure la tension : deux equipes voisines au classement jouent
  serre, un ecart large expose la double chance du mieux classe a domicile ;
- la **solidite des defenses** sur la saison, plus stable que les cinq derniers matchs.

Aucune de ces regles ne predit un resultat : elles disent qu'une selection est plus
fragile qu'elle n'en a l'air, et l'ecartent des combines.
"""

from __future__ import annotations

import re

from betbot.models import MatchStats, TableStanding, TeamForm

BTTS_YES = "Les deux marquent : oui"
BTTS_NO = "Les deux marquent : non"
DOUBLE_CHANCE_HOME = "1N"
DOUBLE_CHANCE_AWAY = "N2"
DOUBLE_CHANCE_NO_DRAW = "12"
# Marches auxquels une regle de piege s'applique.
TRAP_MARKETS = (
    BTTS_YES,
    BTTS_NO,
    DOUBLE_CHANCE_HOME,
    DOUBLE_CHANCE_AWAY,
    DOUBLE_CHANCE_NO_DRAW,
)

# Buts encaisses par match : en dessous la defense tient, au-dessus elle prend l'eau.
SOLID_DEFENCE = 1.0
LEAKY_DEFENCE = 1.7
# Attaque assez fournie pour punir une defense friable.
SHARP_ATTACK = 1.6
# Buts marques par match en dessous desquels une attaque ne suffit pas a porter un
# « les deux marquent : oui » : ce marche exige que les DEUX equipes marquent, c'est
# donc la moins prolifique qui commande. A 1.2 but par match, une equipe reste muette
# environ une rencontre sur trois ; en dessous, c'est davantage.
PROLIFIC_ATTACK = 1.2
# Places de classement : voisins (rencontre tendue) ou eloignes (favori attendu).
TIGHT_TABLE_GAP = 3
WIDE_TABLE_GAP = 8
# Part de nuls au-dela de laquelle exclure le nul devient un pari contre l'habitude.
DRAW_PRONE_SHARE = 0.3
# Buts par confrontation directe : en dessous les deux equipes se neutralisent.
LOW_SCORING_H2H = 2.0
HIGH_SCORING_H2H = 3.0
# Nombre de confrontations directes en dessous duquel la moyenne ne veut rien dire.
MIN_HEAD_TO_HEAD = 2
# Buts au total au-dela desquels une rencontre n'est plus fermee : 0-0, 1-0 et 0-1 se
# jouent sur un but, et c'est la que les pronostics se retournent. Un 1-1 n'entre pas
# dans le compte, les deux equipes y marquant.
CLOSED_GAME_GOALS = 1

# Score d'une confrontation directe, dans un resume « 12.05. Lyon 2-1 Rennes ». Les
# espaces sont indispensables : sans eux la date « 2026-03-02 » se lit comme un score.
_SCORE = re.compile(r"(?<=\s)(\d{1,2})-(\d{1,2})(?=\s)")


def _head_to_head_scores(stats: MatchStats) -> list[tuple[int, int]]:
    """Scores des confrontations directes retenues pour la rencontre."""
    scores = []
    for summary in stats.head_to_head:
        found = _SCORE.findall(summary)
        if found:
            home, away = found[-1]
            scores.append((int(home), int(away)))
    return scores


def head_to_head_goals(stats: MatchStats) -> float | None:
    """Buts par confrontation directe, None si l'echantillon est trop mince."""
    scores = _head_to_head_scores(stats)
    if len(scores) < MIN_HEAD_TO_HEAD:
        return None
    return sum(home + away for home, away in scores) / len(scores)


def head_to_head_draws(stats: MatchStats) -> float | None:
    """Part de nuls dans les confrontations directes, en fraction de 1."""
    scores = _head_to_head_scores(stats)
    if len(scores) < MIN_HEAD_TO_HEAD:
        return None
    return sum(1 for home, away in scores if home == away) / len(scores)


def conceded_per_game(table: TableStanding | None, form: TeamForm | None) -> float | None:
    """Buts encaisses par match, mesures sur la saison sinon sur la forme recente."""
    if table and table.conceded_per_game is not None:
        return table.conceded_per_game
    if form and form.matches_played:
        return form.avg_goals_against
    return None


def scored_per_game(table: TableStanding | None, form: TeamForm | None) -> float | None:
    """Buts marques par match, mesures sur la saison sinon sur la forme recente."""
    if table and table.scored_per_game is not None:
        return table.scored_per_game
    if form and form.matches_played:
        return form.avg_goals_for
    return None


def _draw_share(table: TableStanding | None, form: TeamForm | None) -> float | None:
    if table and table.draw_share is not None:
        return table.draw_share
    if form and form.last_results:
        return form.last_results.count("D") / len(form.last_results)
    return None


def predicted_closed_game(predicted_score: str | None) -> bool:
    """Vrai quand le score pronostique tient en un seul but : 0-0, 1-0 ou 0-1.

    C'est la colonne « pronostic score » de Forebet : quand elle annonce une rencontre
    fermee, tout marche qui reclame des buts des deux cotes joue contre elle.
    """
    if not predicted_score:
        return False
    found = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", predicted_score)
    if not found:
        return False
    home, away = int(found.group(1)), int(found.group(2))
    return home + away <= CLOSED_GAME_GOALS


def _btts_yes_reasons(
    stats: MatchStats, defences: tuple[float | None, float | None], predicted_score: str | None
) -> list[str]:
    reasons = []
    if predicted_closed_game(predicted_score):
        reasons.append(f"score pronostique ferme ({predicted_score.strip()})")
    attacks = (
        scored_per_game(stats.home_table, stats.home_form),
        scored_per_game(stats.away_table, stats.away_form),
    )
    for name, attack in zip((stats.home_team, stats.away_team), attacks, strict=True):
        if attack is not None and attack < PROLIFIC_ATTACK:
            reasons.append(f"attaque de {name} trop tendre ({attack:.1f} but marque par match)")
    home_defence, away_defence = defences
    if (
        home_defence is not None
        and away_defence is not None
        and max(home_defence, away_defence) <= SOLID_DEFENCE
    ):
        reasons.append(
            f"deux defenses solides ({home_defence:.1f} et {away_defence:.1f} buts encaisses "
            "par match)"
        )
    goals = head_to_head_goals(stats)
    if goals is not None and goals < LOW_SCORING_H2H:
        reasons.append(f"confrontations directes fermees ({goals:.1f} but par match)")
    gap = stats.table_gap
    if gap is not None and gap <= TIGHT_TABLE_GAP:
        reasons.append(f"classement serre ({gap} place(s)) : rencontre sous tension")
    return reasons


def _btts_no_reasons(stats: MatchStats, defences: tuple[float | None, float | None]) -> list[str]:
    reasons = []
    names = (stats.home_team, stats.away_team)
    for name, defence in zip(names, defences, strict=True):
        if defence is not None and defence >= LEAKY_DEFENCE:
            reasons.append(f"defense de {name} friable ({defence:.1f} buts encaisses par match)")
    goals = head_to_head_goals(stats)
    if goals is not None and goals > HIGH_SCORING_H2H:
        reasons.append(f"confrontations directes prolifiques ({goals:.1f} buts par match)")
    return reasons


def _no_draw_reasons(stats: MatchStats, predicted_score: str | None) -> list[str]:
    reasons = []
    if predicted_closed_game(predicted_score):
        reasons.append(
            f"score pronostique ferme ({predicted_score.strip()}) : un but suffit a faire le nul"
        )
    shares = (
        _draw_share(stats.home_table, stats.home_form),
        _draw_share(stats.away_table, stats.away_form),
    )
    for name, share in zip((stats.home_team, stats.away_team), shares, strict=True):
        if share is not None and share >= DRAW_PRONE_SHARE:
            reasons.append(f"{name} fait {100 * share:.0f} % de nuls")
    draws = head_to_head_draws(stats)
    if draws is not None and draws >= DRAW_PRONE_SHARE:
        reasons.append(f"{100 * draws:.0f} % de nuls dans les confrontations directes")
    gap = stats.table_gap
    if gap is not None and gap <= TIGHT_TABLE_GAP:
        reasons.append(f"classement serre ({gap} place(s)) : le nul est l'issue naturelle")
    return reasons


def _double_chance_reasons(stats: MatchStats, market: str) -> list[str]:
    """Pieges d'un « ne perd pas » : l'adversaire est meilleur qu'il n'y parait."""
    home, away = stats.home_table, stats.away_table
    protege_home = market == DOUBLE_CHANCE_HOME
    reasons = []
    if home and away:
        behind = (
            (home.position - away.position) if protege_home else (away.position - home.position)
        )
        if behind >= WIDE_TABLE_GAP:
            side = "l'exterieur" if protege_home else "le receveur"
            reasons.append(f"{side} est mieux classe de {behind} places")

    defence = conceded_per_game(
        stats.home_table if protege_home else stats.away_table,
        stats.home_form if protege_home else stats.away_form,
    )
    attack = scored_per_game(
        stats.away_table if protege_home else stats.home_table,
        stats.away_form if protege_home else stats.home_form,
    )
    if (
        defence is not None
        and attack is not None
        and defence >= LEAKY_DEFENCE
        and attack >= SHARP_ATTACK
    ):
        reasons.append(
            f"defense a {defence:.1f} but encaisse face a une attaque a {attack:.1f} but"
        )
    return reasons


def trap_reasons(stats: MatchStats, market: str, predicted_score: str | None = None) -> list[str]:
    """Raisons de considerer cette selection comme un piege, vide s'il n'y en a pas."""
    defences = (
        conceded_per_game(stats.home_table, stats.home_form),
        conceded_per_game(stats.away_table, stats.away_form),
    )
    if market == BTTS_YES:
        return _btts_yes_reasons(stats, defences, predicted_score)
    if market == BTTS_NO:
        return _btts_no_reasons(stats, defences)
    if market == DOUBLE_CHANCE_NO_DRAW:
        return _no_draw_reasons(stats, predicted_score)
    if market in (DOUBLE_CHANCE_HOME, DOUBLE_CHANCE_AWAY):
        return _double_chance_reasons(stats, market)
    return []


def is_trap(stats: MatchStats, market: str, predicted_score: str | None = None) -> bool:
    return bool(trap_reasons(stats, market, predicted_score))
