"""Modele de Poisson bivarie simplifie : contre-expertise chiffree du LLM.

On estime les buts attendus de chaque equipe a partir de sa moyenne offensive et
de la faiblesse defensive adverse, puis on convole les deux lois de Poisson pour
obtenir la matrice des scores exacts.
"""

from __future__ import annotations

from collections.abc import Callable
from math import exp, factorial

from betbot.models import MatchStats, PoissonResult

LEAGUE_AVG_GOALS = 1.35  # buts moyens par equipe et par match (championnats europeens)
HOME_ADVANTAGE = 1.15
MAX_GOALS = 8
SHRINKAGE = 5.0  # pseudo-matchs de regularisation : 5 matchs ne suffisent pas a estimer une force
# Part des buts inscrits avant la pause : les equipes marquent moins en premiere periode.
FIRST_HALF_SHARE = 0.45


# Marches proposes par les bookmakers francais, exprimes comme un predicat sur le
# score exact : (buts domicile, buts exterieur, les deux equipes marquent).
COMBINED_MARKETS: dict[str, Callable[[int, int, bool], bool]] = {
    "1": lambda h, a, _btts: h > a,
    "N": lambda h, a, _btts: h == a,
    "2": lambda h, a, _btts: h < a,
    "1N": lambda h, a, _btts: h >= a,
    "12": lambda h, a, _btts: h != a,
    "N2": lambda h, a, _btts: h <= a,
    "Les deux marquent : oui": lambda h, a, btts: btts,
    "Les deux marquent : non": lambda h, a, btts: not btts,
    "1 et oui": lambda h, a, btts: h > a and btts,
    "2 et oui": lambda h, a, btts: h < a and btts,
    "N et oui": lambda h, a, btts: h == a and btts,
    "1N et oui": lambda h, a, btts: h >= a and btts,
    "12 et oui": lambda h, a, btts: h != a and btts,
    "N2 et oui": lambda h, a, btts: h <= a and btts,
    "1 et non": lambda h, a, btts: h > a and not btts,
    "2 et non": lambda h, a, btts: h < a and not btts,
    "N et non": lambda h, a, btts: h == a and not btts,
    "1N et non": lambda h, a, btts: h >= a and not btts,
    "12 et non": lambda h, a, btts: h != a and not btts,
    "N2 et non": lambda h, a, btts: h <= a and not btts,
}

# Seuils de buts proposes par Unibet en temps reglementaire.
GOAL_LINES = (0.5, 1.5, 2.5, 3.5, 4.5)
# Issues combinables avec un seuil de buts, comme le fait le bookmaker.
_RESULTS: dict[str, Callable[[int, int], bool]] = {
    "1": lambda h, a: h > a,
    "N": lambda h, a: h == a,
    "2": lambda h, a: h < a,
    "1N": lambda h, a: h >= a,
    "12": lambda h, a: h != a,
    "N2": lambda h, a: h <= a,
}


def _goals_markets() -> dict[str, Callable[[int, int, bool], bool]]:
    """Marches de buts : « Plus de 2.5 buts », « 1N et moins de 3.5 buts »..."""
    sides: dict[str, Callable[[int, float], bool]] = {
        "Plus": lambda total, line: total > line,
        "Moins": lambda total, line: total < line,
    }
    markets: dict[str, Callable[[int, int, bool], bool]] = {}
    for line in GOAL_LINES:
        for side, goals in sides.items():
            markets[f"{side} de {line} buts"] = (
                lambda h, a, _btts, ln=line, test=goals: test(h + a, ln)
            )
            for name, result in _RESULTS.items():
                markets[f"{name} et {side.lower()} de {line} buts"] = (
                    lambda h, a, _btts, ln=line, test=goals, keep=result: keep(h, a)
                    and test(h + a, ln)
                )
            markets[f"Les deux marquent : oui et {side.lower()} de {line} buts"] = (
                lambda h, a, btts, ln=line, test=goals: btts and test(h + a, ln)
            )
            markets[f"Les deux marquent : non et {side.lower()} de {line} buts"] = (
                lambda h, a, btts, ln=line, test=goals: not btts and test(h + a, ln)
            )
    return markets


COMBINED_MARKETS.update(_goals_markets())

# Marche de mi-temps : le modele ne convole que le score final, la premiere periode est
# estimee a part avec des buts attendus reduits.
FIRST_HALF_BTTS = "Les deux marquent : oui (1re mi-temps)"


def _poisson_pmf(k: int, lam: float) -> float:
    return exp(-lam) * lam**k / factorial(k)


def _shrink(average: float, sample_size: int) -> float:
    """Rapproche une moyenne d'echantillon de la moyenne de championnat.

    Sur 5 matchs, une moyenne brute est tres bruitee : on la pondere avec
    `SHRINKAGE` pseudo-matchs a la moyenne de la ligue pour eviter des
    probabilites absurdement tranchees.
    """
    if sample_size <= 0:
        return LEAGUE_AVG_GOALS
    weight = sample_size / (sample_size + SHRINKAGE)
    return weight * average + (1 - weight) * LEAGUE_AVG_GOALS


def _expected_goals(attack: float, opponent_defence: float, *, home: bool) -> float:
    attack_strength = (attack or LEAGUE_AVG_GOALS) / LEAGUE_AVG_GOALS
    defence_weakness = (opponent_defence or LEAGUE_AVG_GOALS) / LEAGUE_AVG_GOALS
    expected = attack_strength * defence_weakness * LEAGUE_AVG_GOALS
    if home:
        expected *= HOME_ADVANTAGE
    return max(0.15, min(expected, 5.0))


def compute(stats: MatchStats) -> PoissonResult | None:
    """Calcule les probabilites 1X2, over 2.5 et BTTS a partir des formes recentes."""
    home, away = stats.home_form, stats.away_form
    if not home or not away or not home.matches_played or not away.matches_played:
        return None

    lambda_home = _expected_goals(
        _shrink(home.avg_goals_for, home.matches_played),
        _shrink(away.avg_goals_against, away.matches_played),
        home=True,
    )
    lambda_away = _expected_goals(
        _shrink(away.avg_goals_for, away.matches_played),
        _shrink(home.avg_goals_against, home.matches_played),
        home=False,
    )

    prob_home = prob_draw = prob_away = 0.0
    prob_over_25 = prob_btts = 0.0
    combined = dict.fromkeys(COMBINED_MARKETS, 0.0)
    best_score, best_prob = "0-0", 0.0

    for goals_home in range(MAX_GOALS + 1):
        for goals_away in range(MAX_GOALS + 1):
            probability = _poisson_pmf(goals_home, lambda_home) * _poisson_pmf(
                goals_away, lambda_away
            )

            if goals_home > goals_away:
                prob_home += probability
            elif goals_home == goals_away:
                prob_draw += probability
            else:
                prob_away += probability

            if goals_home + goals_away > 2:
                prob_over_25 += probability
            btts = bool(goals_home and goals_away)
            if btts:
                prob_btts += probability
            for market, predicate in COMBINED_MARKETS.items():
                if predicate(goals_home, goals_away, btts):
                    combined[market] += probability
            if probability > best_prob:
                best_prob, best_score = probability, f"{goals_home}-{goals_away}"

    def percent(value: float) -> float:
        return round(100 * value, 2)

    # Les deux equipes marquent avant la pause : produit des probabilites de marquer au
    # moins une fois sur une periode, buts attendus reduits a leur part de premiere mi-temps.
    half_btts = (1 - exp(-lambda_home * FIRST_HALF_SHARE)) * (
        1 - exp(-lambda_away * FIRST_HALF_SHARE)
    )
    combined[FIRST_HALF_BTTS] = half_btts

    return PoissonResult(
        prob_home=percent(prob_home),
        prob_draw=percent(prob_draw),
        prob_away=percent(prob_away),
        prob_over_25=percent(prob_over_25),
        prob_btts=percent(prob_btts),
        expected_home_goals=round(lambda_home, 2),
        expected_away_goals=round(lambda_away, 2),
        most_likely_score=best_score,
        markets={market: percent(value) for market, value in combined.items()},
    )
