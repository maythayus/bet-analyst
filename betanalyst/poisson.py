"""Modele de Poisson bivarie simplifie : contre-expertise chiffree du LLM.

On estime les buts attendus de chaque equipe a partir de sa moyenne offensive et
de la faiblesse defensive adverse, puis on convole les deux lois de Poisson pour
obtenir la matrice des scores exacts.
"""

from __future__ import annotations

from math import exp, factorial

from betanalyst.models import MatchStats, PoissonResult

LEAGUE_AVG_GOALS = 1.35  # buts moyens par equipe et par match (championnats europeens)
HOME_ADVANTAGE = 1.15
MAX_GOALS = 8
SHRINKAGE = 5.0  # pseudo-matchs de regularisation : 5 matchs ne suffisent pas a estimer une force


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
            if goals_home and goals_away:
                prob_btts += probability
            if probability > best_prob:
                best_prob, best_score = probability, f"{goals_home}-{goals_away}"

    def percent(value: float) -> float:
        return round(100 * value, 2)

    return PoissonResult(
        prob_home=percent(prob_home),
        prob_draw=percent(prob_draw),
        prob_away=percent(prob_away),
        prob_over_25=percent(prob_over_25),
        prob_btts=percent(prob_btts),
        expected_home_goals=round(lambda_home, 2),
        expected_away_goals=round(lambda_away, 2),
        most_likely_score=best_score,
    )
