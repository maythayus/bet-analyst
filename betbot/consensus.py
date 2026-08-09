"""Une seule probabilite par marche, tiree de Forebet et du modele reunis.

Deux estimations independantes valent mieux qu'une, a une condition : qu'elles se
rejoignent. Forebet travaille sur un historique bien plus large que la forme recente ; le
modele de buts, lui, connait les cotes, le lieu, le niveau des adversaires et la
dependance des scores serres. Quand les deux tombent d'accord, la moyenne des deux est
plus juste que chacune isolement, et la certitude est reelle.

Quand elles se contredisent, la moyenne serait le pire des choix : elle fabriquerait une
fausse tranquillite a mi-chemin de deux avis dont l'un se trompe lourdement. Au-dela de
`MAX_DISAGREEMENT` points d'ecart, aucun consensus n'est renvoye et le marche est
simplement ecarte des combines. C'est le meme principe que le detecteur de matchs
pieges : mieux vaut ne rien jouer qu'un chiffre auquel personne ne croit vraiment.
"""

from __future__ import annotations

from dataclasses import dataclass

from betbot.models import MatchBundle

# Poids de Forebet dans la moyenne. Au-dessus de la moitie : son historique couvre des
# saisons entieres, la forme recente vingt matchs.
FOREBET_WEIGHT = 0.6
# Ecart, en points, au-dela duquel les deux sources ne mesurent visiblement pas la meme
# chose. Une difference de vingt points sur un marche a deux issues est un desaccord de
# fond, pas un arrondi.
MAX_DISAGREEMENT = 20.0

SOURCE_CONSENSUS = "Forebet+modele"
SOURCE_FOREBET = "Forebet"
SOURCE_MODEL = "modele"


@dataclass(frozen=True)
class Consensus:
    """Probabilite retenue pour un marche, et ce qui la soutient."""

    probability: float
    source: str
    # Ecart entre les deux estimations, en points. None quand une seule s'est prononcee.
    gap: float | None = None

    @property
    def agreed(self) -> bool:
        """Vrai quand les deux sources se sont prononcees et se rejoignent."""
        return self.source == SOURCE_CONSENSUS


def blend(forebet: float | None, model: float | None) -> Consensus | None:
    """Reunit les deux estimations d'un marche, ou refuse de trancher.

    Renvoie None quand aucune des deux n'existe, et quand elles s'ecartent de plus de
    `MAX_DISAGREEMENT` points : dans ce dernier cas le marche n'est pas jouable, quelle
    que soit la source qu'on prefererait croire.
    """
    if forebet is None and model is None:
        return None
    if forebet is None:
        return Consensus(model or 0.0, SOURCE_MODEL)
    if model is None:
        return Consensus(forebet, SOURCE_FOREBET)

    gap = round(abs(forebet - model), 2)
    if gap > MAX_DISAGREEMENT:
        return None
    probability = FOREBET_WEIGHT * forebet + (1 - FOREBET_WEIGHT) * model
    return Consensus(round(probability, 2), SOURCE_CONSENSUS, gap)


def for_market(bundle: MatchBundle, market: str) -> Consensus | None:
    """Consensus des deux sources sur un marche de la rencontre."""
    forebet = bundle.forebet.markets.get(market) if bundle.forebet else None
    model = bundle.poisson.markets.get(market) if bundle.poisson else None
    return blend(forebet, model)


def summary(bundle: MatchBundle) -> dict[str, dict[str, float | str | bool | None]]:
    """Detail par marche publie par Forebet : les deux valeurs brutes et leur consensus.

    Les deux estimations d'origine sont conservees telles quelles a cote du consensus :
    c'est ce qui permet de voir un desaccord au lieu de le lisser.
    """
    forebet_markets = bundle.forebet.markets if bundle.forebet else {}
    model_markets = bundle.poisson.markets if bundle.poisson else {}
    detail: dict[str, dict[str, float | str | bool | None]] = {}
    for market, forebet in forebet_markets.items():
        model = model_markets.get(market)
        agreed = blend(forebet, model)
        gap = round(abs(forebet - model), 2) if model is not None else None
        detail[market] = {
            "forebet": forebet,
            "modele": model,
            "consensus": agreed.probability if agreed and agreed.agreed else None,
            "ecart": gap,
            "source": agreed.source if agreed else None,
            "desaccord": gap is not None and gap > MAX_DISAGREEMENT,
        }
    return detail
