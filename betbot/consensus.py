"""Une seule probabilite par marche, tiree de Forebet et du modele reunis.

Deux estimations independantes valent mieux qu'une, a une condition : qu'elles se
rejoignent. Forebet travaille sur un historique bien plus large que la forme recente ; le
modele de buts, lui, connait les cotes, le lieu, le niveau des adversaires et la
dependance des scores serres. Quand les deux tombent d'accord, la moyenne des deux est
plus juste que chacune isolement.

Quand elles se contredisent, une moyenne serait le pire des choix : elle fabriquerait une
fausse tranquillite a mi-chemin de deux avis dont l'un se trompe lourdement. Trois
mecanismes traitent ce cas, du plus informe au plus prudent :

1. **La tolerance depend de l'endroit ou tombe l'ecart.** Dix points entre 45 % et 55 %
   font basculer la decision : l'un dit non, l'autre dit oui. Les memes dix points entre
   80 % et 90 % disent la meme chose. La tolerance est donc resserree autour de 50 % et
   relachee aux extremes (`_tolerance`).
2. **Les cotes arbitrent.** Quand les deux sources s'ecartent trop, le bookmaker, lui, a
   un avis, et c'est l'acteur le mieux informe du lot : la source la plus proche de la
   probabilite implicite de sa cote l'emporte, a condition d'etre nettement plus proche.
   Le desaccord devient alors exploitable au lieu d'etre jete.
3. **La confiance se degrade au lieu de casser net.** Plus l'ecart est grand, plus la
   valeur retenue tire vers la plus basse des deux estimations : une selection douteuse
   perd d'elle-meme sa place dans les combines, sans seuil binaire ou 19 points passent
   et 21 points sautent.

Quand rien de tout cela ne tranche, aucun consensus n'est renvoye et le marche est ecarte
des combines : mieux vaut ne rien jouer qu'un chiffre auquel personne ne croit vraiment.
"""

from __future__ import annotations

from dataclasses import dataclass

from betbot.models import MatchBundle

# Poids de Forebet dans la moyenne. Au-dessus de la moitie : son historique couvre des
# saisons entieres, la forme recente vingt matchs.
FOREBET_WEIGHT = 0.6
# Ecart de reference, en points, au-dela duquel les deux sources ne mesurent visiblement
# pas la meme chose. C'est une tolerance moyenne : `_tolerance` la resserre autour de
# 50 %, ou l'ecart fait basculer la decision, et la relache aux extremes.
MAX_DISAGREEMENT = 20.0
# Bornes de cette modulation, en fraction de `MAX_DISAGREEMENT` : 14 points de tolerance
# pour un marche autour de 50 %, 28 points pour un marche presque acquis ou presque exclu.
TOLERANCE_NEAR_EVEN = 0.7
TOLERANCE_AT_EXTREME = 1.4
# Ecart minimal, en points, dont une source doit etre plus proche de la cote que l'autre
# pour que le bookmaker soit dit avoir tranche. En dessous, les deux sont aussi credibles
# et le marche reste ecarte.
ARBITRATION_MARGIN = 5.0

SOURCE_CONSENSUS = "Forebet+modele"
SOURCE_FOREBET = "Forebet"
SOURCE_MODEL = "modele"
# Suffixe ajoute a la source quand c'est la cote qui a departage les deux estimations.
ARBITRATED = " (cote arbitre)"

# Issue complementaire de chaque marche, pour retirer la marge du bookmaker : deux cotes
# d'issues exhaustives et exclusives donnent une probabilite implicite propre.
_COMPLEMENTS = {
    "Les deux marquent : oui": "Les deux marquent : non",
    "1N": "2",
    "N2": "1",
    "12": "N",
}
COMPLEMENTS = {**_COMPLEMENTS, **{value: key for key, value in _COMPLEMENTS.items()}}
COMPLEMENTS.update(
    {f"Plus de {line} buts": f"Moins de {line} buts" for line in (0.5, 1.5, 2.5, 3.5, 4.5)}
)
COMPLEMENTS.update(
    {f"Moins de {line} buts": f"Plus de {line} buts" for line in (0.5, 1.5, 2.5, 3.5, 4.5)}
)


@dataclass(frozen=True)
class Consensus:
    """Probabilite retenue pour un marche, et ce qui la soutient."""

    probability: float
    source: str
    # Ecart entre les deux estimations, en points. None quand une seule s'est prononcee.
    gap: float | None = None
    # Confiance dans la valeur retenue, de 0 a 1 : 1 quand les deux sources disent la
    # meme chose, 0 a la limite de la tolerance. None quand une seule s'est prononcee.
    confidence: float | None = None

    @property
    def agreed(self) -> bool:
        """Vrai quand les deux sources se sont prononcees et se rejoignent."""
        return self.source == SOURCE_CONSENSUS

    @property
    def arbitrated(self) -> bool:
        """Vrai quand c'est la cote du bookmaker qui a departage les deux sources."""
        return self.source.endswith(ARBITRATED)


def _tolerance(forebet: float, model: float) -> float:
    """Ecart tolere entre les deux sources, selon l'endroit ou se situe le marche.

    Un desaccord ne coute pas la meme chose partout : autour de 50 %, dix points font
    passer une selection de « non jouable » a « jouable », alors que les memes dix points
    entre 80 % et 90 % laissent les deux sources d'accord sur la conclusion.
    """
    middle = (forebet + model) / 2
    distance = min(abs(middle - 50.0) / 50.0, 1.0)
    span = TOLERANCE_AT_EXTREME - TOLERANCE_NEAR_EVEN
    return MAX_DISAGREEMENT * (TOLERANCE_NEAR_EVEN + span * distance)


def implied_for_market(bundle: MatchBundle, market: str) -> float | None:
    """Probabilite implicite de la cote du marche, marge retiree quand c'est possible.

    Quand l'issue complementaire est cotee elle aussi, la marge du bookmaker est repartie
    entre les deux. Sinon la valeur brute `100 / cote` est renvoyee : elle surestime
    l'issue de la marge du bookmaker, ce qui reste utilisable pour comparer deux
    estimations mais ne vaut pas une probabilite.
    """
    prices = bundle.market_prices()
    odds = prices.get(market)
    if not odds or odds <= 1:
        return None
    other = prices.get(COMPLEMENTS.get(market, ""))
    if other and other > 1:
        overround = 1 / odds + 1 / other
        return round(100 / odds / overround, 2)
    return round(100 / odds, 2)


def _arbitrate(forebet: float, model: float, implied: float | None) -> Consensus | None:
    """Departage deux estimations en desaccord grace a la cote du bookmaker.

    Le marche est l'acteur le mieux informe des trois : la source qui s'en approche le
    plus est retenue telle quelle, sans moyenne, et a condition d'etre nettement plus
    proche. Sa probabilite brute est conservee, mais la confiance reste nulle : une
    source a eu tort de beaucoup sur ce marche, l'autre n'est pas verifiee pour autant.
    """
    if implied is None:
        return None
    gap = round(abs(forebet - model), 2)
    forebet_error, model_error = abs(forebet - implied), abs(model - implied)
    if abs(forebet_error - model_error) < ARBITRATION_MARGIN:
        return None
    if forebet_error < model_error:
        return Consensus(forebet, SOURCE_FOREBET + ARBITRATED, gap, 0.0)
    return Consensus(model, SOURCE_MODEL + ARBITRATED, gap, 0.0)


def blend(
    forebet: float | None, model: float | None, implied: float | None = None
) -> Consensus | None:
    """Reunit les deux estimations d'un marche, ou refuse de trancher.

    `implied` est la probabilite implicite de la cote, quand elle est connue : elle sert
    d'arbitre en cas de desaccord. Renvoie None quand aucune des deux sources n'existe,
    et quand elles se contredisent sans que la cote puisse departager : le marche n'est
    alors pas jouable, quelle que soit la source qu'on prefererait croire.
    """
    if forebet is None and model is None:
        return None
    if forebet is None:
        return Consensus(model or 0.0, SOURCE_MODEL)
    if model is None:
        return Consensus(forebet, SOURCE_FOREBET)

    gap = round(abs(forebet - model), 2)
    tolerance = _tolerance(forebet, model)
    if gap > tolerance:
        return _arbitrate(forebet, model, implied)

    # La moyenne ponderee est ensuite tiree vers la plus basse des deux estimations, a
    # proportion du desaccord : a ecart nul elle est intacte, a la limite de la tolerance
    # il ne reste que la valeur prudente.
    average = FOREBET_WEIGHT * forebet + (1 - FOREBET_WEIGHT) * model
    doubt = gap / tolerance
    probability = average - (average - min(forebet, model)) * doubt
    return Consensus(round(probability, 2), SOURCE_CONSENSUS, gap, round(1 - doubt, 2))


def for_market(bundle: MatchBundle, market: str) -> Consensus | None:
    """Consensus des deux sources sur un marche de la rencontre, la cote en arbitre."""
    forebet = bundle.forebet.markets.get(market) if bundle.forebet else None
    model = bundle.poisson.markets.get(market) if bundle.poisson else None
    return blend(forebet, model, implied_for_market(bundle, market))


def summary(bundle: MatchBundle) -> dict[str, dict[str, float | str | bool | None]]:
    """Detail par marche publie par Forebet : les valeurs brutes et ce qu'on en a tire.

    Les deux estimations d'origine sont conservees telles quelles a cote du consensus :
    c'est ce qui permet de voir un desaccord au lieu de le lisser.
    """
    forebet_markets = bundle.forebet.markets if bundle.forebet else {}
    model_markets = bundle.poisson.markets if bundle.poisson else {}
    detail: dict[str, dict[str, float | str | bool | None]] = {}
    for market, forebet in forebet_markets.items():
        model = model_markets.get(market)
        implied = implied_for_market(bundle, market)
        agreed = blend(forebet, model, implied)
        gap = round(abs(forebet - model), 2) if model is not None else None
        detail[market] = {
            "forebet": forebet,
            "modele": model,
            "cote_implicite": implied,
            "retenu": agreed.probability if agreed else None,
            "ecart": gap,
            "tolerance": round(_tolerance(forebet, model), 2) if model is not None else None,
            "source": agreed.source if agreed else None,
            "confiance": agreed.confidence if agreed else None,
            "desaccord": agreed is None or agreed.arbitrated,
        }
    return detail
