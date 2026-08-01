"""Modele de buts : deux estimations au choix, la forme seule ou l'ancrage sur les cotes.

Deux sources d'information existent pour estimer les buts attendus d'une rencontre :
les cotes du bookmaker, qui agregent l'information de tout le marche, et la forme
recente lue sur Flashscore, qui porte sur cinq matchs et ignore le niveau des
adversaires. La seconde, utilisee seule, produit des estimations absurdes des qu'une
equipe change de contexte (un club qui marque 2.6 buts par match dans son championnat
n'est pas favori en coupe d'Europe).

Le modele part donc des cotes quand elles existent : on retire la marge, puis on
cherche le couple de buts attendus dont la matrice des scores reproduit au mieux les
marches cotes (1 N 2, seuils de buts, les deux marquent). La forme n'apporte plus
qu'un ajustement borne. Sans cotes, on retombe sur la forme seule, fortement
regularisee, et le resultat est signale comme peu fiable.

La matrice des scores applique la correction Dixon-Coles : deux lois de Poisson
independantes sous-estiment les scores nuls et serres, donc la probabilite qu'une
equipe ne marque pas.

Les deux modeles restent disponibles, `MODEL_FORM` etant celui d'origine :

- `MODEL_FORM` : buts attendus deduits de la seule forme recente, sans correction
  Dixon-Coles ni reference au marche. Il produit des probabilites nettement plus
  tranchees que les cotes, donc beaucoup de valeur apparente, dont une partie est de
  l'erreur d'estimation ;
- `MODEL_MARKET` : la meme matrice, mais calee sur les cotes dont la marge a ete
  retiree. Il reproduit le marche a environ deux points pres, et ne trouve donc que
  rarement un pari a esperance positive.

Dans les deux cas l'ecart aux probabilites du marche est calcule et publie : c'est la
seule facon de lire un chiffre du premier modele sans se raconter d'histoire.
"""

from __future__ import annotations

from collections.abc import Callable
from math import exp, factorial

from betbot.models import MatchStats, PoissonResult

LEAGUE_AVG_GOALS = 1.35  # buts moyens par equipe et par match (championnats europeens)
HOME_ADVANTAGE = 1.15
MAX_GOALS = 8
SHRINKAGE = 8.0  # pseudo-matchs de regularisation : 5 matchs ne suffisent pas a estimer une force
# Regularisation et bornes du modele de forme d'origine, plus permissives : elles le
# laissent atteindre des buts attendus que les cotes ne soutiennent pas.
FORM_SHRINKAGE = 5.0
FORM_MIN_LAMBDA, FORM_MAX_LAMBDA = 0.15, 5.0
# Dependance entre les deux scores : negatif, il ramene du poids sur 0-0 et 1-1, que
# deux Poisson independantes sous-estiment. Valeur usuelle de la litterature.
RHO = -0.06
# Bornes des buts attendus : au-dela, le modele extrapole une forme, il ne mesure plus.
MIN_LAMBDA, MAX_LAMBDA = 0.20, 3.60
# Poids de la forme quand les cotes sont disponibles, et ecart maximal autorise.
FORM_WEIGHT = 0.25
FORM_MAX_TILT = 0.12
# Part des buts inscrits avant la pause : les equipes marquent moins en premiere periode.
FIRST_HALF_SHARE = 0.45

SOURCE_MARKET = "cotes"
SOURCE_MARKET_FORM = "cotes + forme"
SOURCE_FORM = "forme seule"
SOURCE_FORM_ONLY = "forme recente"

# Modeles selectionnables : le premier est celui des premieres versions de Bet.Bot.
MODEL_FORM = "forme"
MODEL_MARKET = "marche"
MODELS = (MODEL_FORM, MODEL_MARKET)
DEFAULT_MODEL = MODEL_FORM

# Sources issues d'une calibration sur les cotes : leurs valeurs sont comparables au
# marche, celles du modele de forme ne le sont pas.
CALIBRATED_SOURCES = (SOURCE_MARKET, SOURCE_MARKET_FORM)


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

# Marches sur lesquels le modele se cale quand ils sont cotes. Chaque entree est un
# groupe d'issues exhaustives et exclusives : leurs cotes suffisent a retirer la marge.
# Les seuils extremes (0.5, 4.5 buts) portent une marge enorme sur l'issue improbable :
# leur prix renseigne mal sur les buts attendus, ils sont exclus de la calibration.
_CALIBRATION_LINES = (1.5, 2.5, 3.5)
_CALIBRATION_GROUPS: list[tuple[float, tuple[str, ...]]] = [
    (3.0, ("1", "N", "2")),
    *[(1.0, (f"Plus de {line} buts", f"Moins de {line} buts")) for line in _CALIBRATION_LINES],
    (1.5, ("Les deux marquent : oui", "Les deux marquent : non")),
]


def _poisson_pmf(k: int, lam: float) -> float:
    return exp(-lam) * lam**k / factorial(k)


def _tau(goals_home: int, goals_away: int, lambda_home: float, lambda_away: float) -> float:
    """Correction Dixon-Coles des scores serres, laissee a 1 au-dela de 1-1."""
    if goals_home == 0 and goals_away == 0:
        return 1 - lambda_home * lambda_away * RHO
    if goals_home == 0 and goals_away == 1:
        return 1 + lambda_home * RHO
    if goals_home == 1 and goals_away == 0:
        return 1 + lambda_away * RHO
    if goals_home == 1 and goals_away == 1:
        return 1 - RHO
    return 1.0


def score_matrix(
    lambda_home: float, lambda_away: float, *, dixon_coles: bool = True
) -> list[list[float]]:
    """Probabilite de chaque score exact, correction Dixon-Coles incluse par defaut."""
    rows = [
        [
            _poisson_pmf(home, lambda_home)
            * _poisson_pmf(away, lambda_away)
            * (max(_tau(home, away, lambda_home, lambda_away), 0.0) if dixon_coles else 1.0)
            for away in range(MAX_GOALS + 1)
        ]
        for home in range(MAX_GOALS + 1)
    ]
    total = sum(sum(row) for row in rows)
    return [[value / total for value in row] for row in rows]


def _market_probabilities(matrix: list[list[float]]) -> dict[str, float]:
    """Probabilite de chaque marche combine, en fraction de 1."""
    probabilities = dict.fromkeys(COMBINED_MARKETS, 0.0)
    for home, row in enumerate(matrix):
        for away, probability in enumerate(row):
            btts = bool(home and away)
            for market, predicate in COMBINED_MARKETS.items():
                if predicate(home, away, btts):
                    probabilities[market] += probability
    return probabilities


def devig(odds: dict[str, float], outcomes: tuple[str, ...]) -> dict[str, float] | None:
    """Probabilites d'un groupe d'issues exhaustives, marge du bookmaker retiree.

    La marge est repartie proportionnellement a la probabilite implicite de chaque
    issue. La somme obtenue vaut 1 : ce sont les probabilites que le marche affiche
    reellement, une fois retiree la commission.
    """
    if not all(odds.get(outcome) for outcome in outcomes):
        return None
    raw = {outcome: 1 / odds[outcome] for outcome in outcomes}
    overround = sum(raw.values())
    if overround <= 0:
        return None
    return {outcome: value / overround for outcome, value in raw.items()}


def _market_targets(odds: dict[str, float]) -> list[tuple[float, str, float]]:
    """Cibles de calibration `(poids, marche, probabilite du marche)`."""
    targets: list[tuple[float, str, float]] = []
    for weight, outcomes in _CALIBRATION_GROUPS:
        probabilities = devig(odds, outcomes)
        if not probabilities:
            continue
        targets.extend(
            (weight, outcome, probability) for outcome, probability in probabilities.items()
        )
    return targets


def _fit_error(
    lambda_home: float, lambda_away: float, targets: list[tuple[float, str, float]]
) -> float:
    model = _market_probabilities(score_matrix(lambda_home, lambda_away))
    return sum(
        weight * (model[market] - probability) ** 2 for weight, market, probability in targets
    )


def fit_from_market(odds: dict[str, float]) -> tuple[float, float, float] | None:
    """Buts attendus reproduisant au mieux les marches cotes.

    Retourne `(buts domicile, buts exterieur, ecart maximal en points)`. La recherche
    est une descente sur grille : deux parametres, une fonction lisse, aucune
    dependance numerique supplementaire.
    """
    targets = _market_targets(odds)
    if not targets:
        return None

    best = (LEAGUE_AVG_GOALS * HOME_ADVANTAGE, LEAGUE_AVG_GOALS)
    best_error = _fit_error(*best, targets)
    step = 0.8
    while step > 0.005:
        improved = False
        for delta_home in (-step, 0.0, step):
            for delta_away in (-step, 0.0, step):
                if not delta_home and not delta_away:
                    continue
                candidate = (
                    min(max(best[0] + delta_home, MIN_LAMBDA), MAX_LAMBDA),
                    min(max(best[1] + delta_away, MIN_LAMBDA), MAX_LAMBDA),
                )
                error = _fit_error(*candidate, targets)
                if error < best_error - 1e-12:
                    best, best_error, improved = candidate, error, True
        if not improved:
            step /= 2

    gap = market_gap(_market_probabilities(score_matrix(*best)), odds)
    return best[0], best[1], gap or 0.0


def market_gap(markets: dict[str, float], odds: dict[str, float] | None) -> float | None:
    """Ecart maximal, en points, entre les marches du modele et ceux du bookmaker.

    Calcule quel que soit le modele : un ecart de trente points ne veut pas dire que
    trente points de valeur sont a prendre, mais que l'une des deux estimations se
    trompe lourdement, et ce n'est pas toujours celle du bookmaker.
    """
    targets = _market_targets(odds) if odds else []
    if not targets:
        return None
    gap = max(abs(markets.get(market, 0.0) - probability) for _w, market, probability in targets)
    return round(100 * gap, 2)


def _shrink(average: float, sample_size: int, shrinkage: float = SHRINKAGE) -> float:
    """Rapproche une moyenne d'echantillon de la moyenne de championnat.

    Sur cinq matchs, une moyenne brute est tres bruitee et ignore le niveau des
    adversaires rencontres : on la pondere avec `SHRINKAGE` pseudo-matchs a la moyenne
    de la ligue pour eviter des probabilites absurdement tranchees.
    """
    if sample_size <= 0:
        return LEAGUE_AVG_GOALS
    weight = sample_size / (sample_size + shrinkage)
    return weight * average + (1 - weight) * LEAGUE_AVG_GOALS


def _expected_goals(
    attack: float,
    opponent_defence: float,
    *,
    home: bool,
    bounds: tuple[float, float] = (MIN_LAMBDA, MAX_LAMBDA),
) -> float:
    attack_strength = (attack or LEAGUE_AVG_GOALS) / LEAGUE_AVG_GOALS
    defence_weakness = (opponent_defence or LEAGUE_AVG_GOALS) / LEAGUE_AVG_GOALS
    expected = attack_strength * defence_weakness * LEAGUE_AVG_GOALS
    if home:
        expected *= HOME_ADVANTAGE
    return min(max(expected, bounds[0]), bounds[1])


def fit_from_form(
    stats: MatchStats,
    *,
    shrinkage: float = SHRINKAGE,
    bounds: tuple[float, float] = (MIN_LAMBDA, MAX_LAMBDA),
) -> tuple[float, float] | None:
    """Buts attendus deduits de la forme recente, faute de cotes."""
    home, away = stats.home_form, stats.away_form
    if not home or not away or not home.matches_played or not away.matches_played:
        return None
    return (
        _expected_goals(
            _shrink(home.avg_goals_for, home.matches_played, shrinkage),
            _shrink(away.avg_goals_against, away.matches_played, shrinkage),
            home=True,
            bounds=bounds,
        ),
        _expected_goals(
            _shrink(away.avg_goals_for, away.matches_played, shrinkage),
            _shrink(home.avg_goals_against, home.matches_played, shrinkage),
            home=False,
            bounds=bounds,
        ),
    )


def _tilt(anchor: float, form: float) -> float:
    """Ajustement borne d'un buts attendus de marche par la forme recente.

    La forme ne peut deplacer l'estimation que de `FORM_MAX_TILT` au plus : c'est de la
    ou peut venir un ecart avec le marche, sans laisser cinq matchs de championnat
    dicter une probabilite que tout le marche contredit.
    """
    ratio = form / anchor if anchor else 1.0
    ratio = 1 + FORM_WEIGHT * (ratio - 1)
    ratio = min(max(ratio, 1 - FORM_MAX_TILT), 1 + FORM_MAX_TILT)
    return min(max(anchor * ratio, MIN_LAMBDA), MAX_LAMBDA)


def compute(
    stats: MatchStats,
    market_odds: dict[str, float] | None = None,
    *,
    model: str = DEFAULT_MODEL,
) -> PoissonResult | None:
    """Probabilites de tous les marches, selon le modele demande."""
    if model == MODEL_FORM:
        return _compute_from_form(stats, market_odds)

    anchor = fit_from_market(market_odds) if market_odds else None
    form = fit_from_form(stats)

    calibration_gap: float | None = None
    if anchor:
        lambda_home, lambda_away, calibration_gap = anchor
        source = SOURCE_MARKET
        if form:
            lambda_home = _tilt(lambda_home, form[0])
            lambda_away = _tilt(lambda_away, form[1])
            source = SOURCE_MARKET_FORM
    elif form:
        lambda_home, lambda_away = form
        source = SOURCE_FORM
    else:
        return None

    return _result(lambda_home, lambda_away, source, calibration_gap, dixon_coles=True)


def _compute_from_form(
    stats: MatchStats, market_odds: dict[str, float] | None
) -> PoissonResult | None:
    """Modele d'origine : deux Poisson independantes nourries par la forme recente.

    Les cotes ne servent qu'a mesurer l'ecart obtenu, jamais a corriger l'estimation :
    c'est le comportement des premieres versions, celui qui produit des probabilites
    tranchees et donc des combines a forte valeur affichee.
    """
    form = fit_from_form(
        stats, shrinkage=FORM_SHRINKAGE, bounds=(FORM_MIN_LAMBDA, FORM_MAX_LAMBDA)
    )
    if not form:
        return None
    result = _result(*form, SOURCE_FORM_ONLY, None, dixon_coles=False)
    result.calibration_gap = market_gap(
        {market: value / 100 for market, value in result.markets.items()}, market_odds
    )
    return result


def _result(
    lambda_home: float,
    lambda_away: float,
    source: str,
    calibration_gap: float | None,
    *,
    dixon_coles: bool,
) -> PoissonResult:
    """Tous les marches derives d'un couple de buts attendus."""
    matrix = score_matrix(lambda_home, lambda_away, dixon_coles=dixon_coles)
    combined = _market_probabilities(matrix)
    best_score, best_probability = "0-0", 0.0
    for home, row in enumerate(matrix):
        for away, probability in enumerate(row):
            if probability > best_probability:
                best_probability, best_score = probability, f"{home}-{away}"

    # Les deux equipes marquent avant la pause : produit des probabilites de marquer au
    # moins une fois sur une periode, buts attendus reduits a leur part de premiere mi-temps.
    combined[FIRST_HALF_BTTS] = (1 - exp(-lambda_home * FIRST_HALF_SHARE)) * (
        1 - exp(-lambda_away * FIRST_HALF_SHARE)
    )

    def percent(value: float) -> float:
        return round(100 * value, 2)

    return PoissonResult(
        prob_home=percent(combined["1"]),
        prob_draw=percent(combined["N"]),
        prob_away=percent(combined["2"]),
        prob_over_25=percent(combined["Plus de 2.5 buts"]),
        prob_btts=percent(combined["Les deux marquent : oui"]),
        expected_home_goals=round(lambda_home, 2),
        expected_away_goals=round(lambda_away, 2),
        most_likely_score=best_score,
        markets={market: percent(value) for market, value in combined.items()},
        source=source,
        calibration_gap=calibration_gap,
    )
