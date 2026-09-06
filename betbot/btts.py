"""Taux empirique « les deux marquent », tire du detail des matchs Flashscore.

Le modele de Poisson deduit la probabilite que les deux equipes marquent de leurs
moyennes de buts ; Forebet publie la sienne. Ni l'un ni l'autre ne regarde ce que les vingt
derniers matchs disent directement : combien de fois l'equipe a marque, combien de fois
elle a encaisse, et combien de fois les deux camps ont trouve la faille. Ce comptage est
le predicteur le plus simple et le plus fiable de ce marche, parce qu'il ne suppose rien
sur la forme de la distribution des buts.

La rencontre a venir voit les deux equipes marquer si le receveur marque ET si le
visiteur marque. Chacune de ces deux frequences est estimee par la moyenne de deux
lectures : la part de matchs ou l'attaque en question a marque, et la part de matchs ou la
defense adverse a encaisse. Les confrontations directes, quand il y en a assez, tirent
ensuite l'estimation vers ce que ces deux equipes font quand elles se rencontrent.

Les ponderations sont celles de `betbot.strength` : les matchs recents et ceux joues dans
le meme contexte (domicile pour le receveur, exterieur pour le visiteur) pesent plus.
"""

from __future__ import annotations

from dataclasses import dataclass

from betbot.models import MatchStats, TeamForm
from betbot.strength import RECENCY_DECAY, VENUE_WEIGHT
from betbot.trap import head_to_head_scores

# Matchs detailles au minimum pour qu'un comptage veuille dire quelque chose.
MIN_MATCHES = 5
# Poids des confrontations directes, en matchs equivalents : avec quatre face-a-face, ils
# pesent autant que la forme des deux equipes.
HEAD_TO_HEAD_PRIOR = 4.0
# Confrontations directes en dessous desquelles elles ne comptent pas.
MIN_HEAD_TO_HEAD = 2


@dataclass(frozen=True)
class TeamShares:
    """Frequences d'une equipe sur ses derniers matchs, en fraction de 1."""

    scored: float  # part de matchs ou elle a marque
    conceded: float  # part de matchs ou elle a encaisse
    both: float  # part de matchs ou les deux equipes ont marque
    sample: float  # matchs equivalents apres ponderation


@dataclass(frozen=True)
class EmpiricalBtts:
    """Probabilite empirique que les deux equipes marquent, et ce qui la fonde."""

    probability: float  # en %
    home_scores: float  # probabilite que le receveur marque, en %
    away_scores: float  # probabilite que le visiteur marque, en %
    head_to_head: float | None  # part de face-a-face ou les deux ont marque, en %
    home: TeamShares
    away: TeamShares


def team_shares(form: TeamForm | None, *, at_home: bool) -> TeamShares | None:
    """Frequences ponderees d'une equipe, None sans detail match par match suffisant."""
    if not form or len(form.matches) < MIN_MATCHES:
        return None
    scored = conceded = both = total = 0.0
    for rank, match in enumerate(form.matches):
        weight = RECENCY_DECAY**rank * (VENUE_WEIGHT if match.at_home == at_home else 1.0)
        scored += weight * (match.scored > 0)
        conceded += weight * (match.conceded > 0)
        both += weight * (match.scored > 0 and match.conceded > 0)
        total += weight
    if not total:
        return None
    return TeamShares(scored / total, conceded / total, both / total, total)


def head_to_head_share(stats: MatchStats) -> float | None:
    """Part de confrontations directes ou les deux equipes ont marque, en fraction de 1."""
    scores = head_to_head_scores(stats)
    if len(scores) < MIN_HEAD_TO_HEAD:
        return None
    return sum(1 for home, away in scores if home > 0 and away > 0) / len(scores)


def empirical_btts(stats: MatchStats) -> EmpiricalBtts | None:
    """Taux empirique « les deux marquent » de la rencontre, None faute de detail."""
    home = team_shares(stats.home_form, at_home=True)
    away = team_shares(stats.away_form, at_home=False)
    if home is None or away is None:
        return None

    home_scores = (home.scored + away.conceded) / 2
    away_scores = (away.scored + home.conceded) / 2
    probability = home_scores * away_scores

    h2h = head_to_head_share(stats)
    if h2h is not None:
        count = len(head_to_head_scores(stats))
        share = count / (count + HEAD_TO_HEAD_PRIOR)
        probability = (1 - share) * probability + share * h2h

    return EmpiricalBtts(
        probability=round(100 * probability, 2),
        home_scores=round(100 * home_scores, 2),
        away_scores=round(100 * away_scores, 2),
        head_to_head=round(100 * h2h, 2) if h2h is not None else None,
        home=home,
        away=away,
    )
