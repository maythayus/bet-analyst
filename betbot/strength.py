"""Force d'attaque et de defense d'une equipe, mesuree sur ses derniers matchs.

Une moyenne de buts brute sur cinq matchs est le point faible de tout modele de forme :
elle compte a l'identique un match d'il y a une semaine et un match d'il y a trois mois,
un deplacement chez le leader et une reception du dernier, et un match a domicile et un
match a l'exterieur. Quatre corrections sont appliquees ici, dans cet ordre :

1. **Anciennete** : chaque match plus vieux pese `RECENCY_DECAY` fois le precedent. Vingt
   matchs apportent alors de la matiere sans que la saison passee dicte la forme du jour.
2. **Lieu** : les matchs joues dans le meme contexte que la rencontre a venir (a domicile
   pour l'equipe qui recoit) comptent `VENUE_WEIGHT` fois plus. La forme a domicile n'est
   pas la forme a l'exterieur, et c'est parfois tout l'ecart.
3. **Force de l'adversaire** : les buts marques sont divises par la permeabilite de la
   defense affrontee, les buts encaisses par le tranchant de l'attaque affrontee, ces
   deux facteurs venant du classement de la saison. Trois buts contre le dernier valent
   alors moins que trois buts contre le leader.
4. **Ancrage sur la saison** : la mesure est rapprochee des buts de la saison entiere
   (classement) plutot que de la seule moyenne de championnat, et d'autant plus fort que
   l'echantillon est mince.

Rien de tout cela n'invente d'information : ces corrections rendent les probabilites plus
justes, pas plus elevees. Un match indecis le reste.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from betbot.models import PlayedMatch, TableStanding, TeamForm
from betbot.names import similarity

# Poids d'un match par rang d'anciennete : le dixieme match compte moitie moins que le
# dernier joue.
RECENCY_DECAY = 0.93
# Un match joue dans le meme contexte (domicile ou exterieur) que la rencontre a venir.
VENUE_WEIGHT = 1.6
# Bornes du facteur adversaire : le classement d'une equipe reste une mesure grossiere,
# on ne lui laisse pas doubler ou annuler une performance.
MIN_OPPONENT_FACTOR, MAX_OPPONENT_FACTOR = 0.65, 1.55
# Ressemblance minimale pour reconnaitre un adversaire dans le classement.
NAME_MATCH = 0.72
# Poids, en matchs equivalents, de la saison entiere comme point de depart.
SEASON_PRIOR = 6.0
# Buts moyens par equipe et par match, utilises quand aucun classement n'est disponible.
LEAGUE_AVG_GOALS = 1.35


@dataclass(frozen=True)
class Rates:
    """Buts marques et encaisses par match, corriges de la force des adversaires."""

    scored: float
    conceded: float
    # Nombre de matchs equivalents derriere la mesure, apres ponderation. Sert a juger
    # combien de credit lui accorder.
    sample: float


def _table_rates(row: TableStanding | None) -> tuple[float, float] | None:
    """Buts marques et encaisses par match sur la saison, si le classement les donne."""
    if not row:
        return None
    scored, conceded = row.scored_per_game, row.conceded_per_game
    if scored is None or conceded is None:
        return None
    return scored, conceded


def find_standing(name: str, standings: Iterable[TableStanding]) -> TableStanding | None:
    """Ligne de classement d'une equipe, reconnue par ressemblance de nom.

    Les noms d'adversaires lus sur une page de resultats sont abreges autrement que ceux
    du classement (« Man City » contre « Manchester City ») : une egalite stricte ne
    reconnaitrait presque personne.
    """
    rows = list(standings)
    if not rows:
        return None
    best = max(rows, key=lambda row: similarity(name, row.name))
    return best if similarity(name, best.name) >= NAME_MATCH else None


def opponent_factors(
    opponent: str, standings: Iterable[TableStanding]
) -> tuple[float, float] | None:
    """`(permeabilite de sa defense, tranchant de son attaque)`, 1.0 valant la moyenne.

    Renvoie None quand l'adversaire est introuvable au classement : un match de coupe
    contre un club d'une autre division ne doit alors pas etre corrige au hasard.
    """
    rows = list(standings)
    row = find_standing(opponent, rows)
    rates = _table_rates(row)
    if not rates:
        return None
    average = _league_average(rows)
    scored, conceded = rates

    def bounded(value: float) -> float:
        return min(max(value / average, MIN_OPPONENT_FACTOR), MAX_OPPONENT_FACTOR)

    return bounded(conceded), bounded(scored)


def _league_average(standings: Iterable[TableStanding]) -> float:
    """Buts par equipe et par match dans la competition, sinon la moyenne europeenne."""
    played = sum(row.played for row in standings)
    scored = sum(row.goals_for for row in standings)
    return scored / played if played else LEAGUE_AVG_GOALS


def _weights(matches: list[PlayedMatch], *, at_home: bool) -> list[float]:
    """Poids de chaque match : anciennete d'abord, lieu ensuite."""
    return [
        RECENCY_DECAY**rank * (VENUE_WEIGHT if match.at_home == at_home else 1.0)
        for rank, match in enumerate(matches)
    ]


def team_rates(
    form: TeamForm | None,
    *,
    at_home: bool,
    table: TableStanding | None = None,
    standings: Iterable[TableStanding] = (),
) -> Rates | None:
    """Buts marques et encaisses par match, ponderes, corriges et ancres sur la saison.

    Sans detail match par match (`TeamForm.matches` vide), seules les moyennes brutes
    sont disponibles : elles sont renvoyees telles quelles, comme avant.
    """
    if not form or not form.matches_played:
        return None
    rows = list(standings)
    if not form.matches:
        return Rates(form.avg_goals_for, form.avg_goals_against, float(form.matches_played))

    weights = _weights(form.matches, at_home=at_home)
    scored = conceded = total = 0.0
    for match, weight in zip(form.matches, weights, strict=True):
        factors = opponent_factors(match.opponent, rows)
        leaky, sharp = factors or (1.0, 1.0)
        scored += weight * match.scored / leaky
        conceded += weight * match.conceded / sharp
        total += weight
    if not total:
        return None

    measured = Rates(scored / total, conceded / total, total)
    season = _table_rates(table)
    if not season:
        return measured
    # La saison entiere sert de point de depart : l'ecart de la forme recente y est
    # ajoute d'autant plus que l'echantillon est fourni.
    share = total / (total + SEASON_PRIOR)
    return Rates(
        share * measured.scored + (1 - share) * season[0],
        share * measured.conceded + (1 - share) * season[1],
        total,
    )
