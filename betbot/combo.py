"""Construction d'un ticket combine a partir des probabilites du modele.

Les selections retenues sont les plus probables du jour, une par match. La
probabilite d'un combine est le produit des probabilites de ses selections : elle
s'effondre tres vite, et c'est precisement ce que le module rend visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import prod

from betbot.models import MatchBundle

# Chez les bookmakers le nul s'ecrit X, dans le modele il s'ecrit N.
_SIGN_TO_MARKET = {"X": "N"}

# Tailles de combines proposees en fin de rapport, en plus du ticket principal.
VALUE_TICKET_SIZES = (6, 8)
# Une selection a moins d'une chance sur deux n'a rien a faire dans un combine long :
# huit selections a 50 % ne passent qu'une fois sur 256.
MIN_LEG_PROBABILITY = 55.0
# Au-dela de cet ecart avec le marche, l'explication la plus probable n'est pas une
# aubaine mais une erreur du modele (equipe mal identifiee, statistiques manquantes) :
# ces selections sont ecartees des combines plutot que recherchees.
MAX_LEG_VALUE = 25.0
# En dessous de cette cote, l'issue est une quasi-certitude que le bookmaker vend a
# perte de temps (« plus de 0.5 but » a 1.03) : elle occupe la premiere place du
# classement par valeur sans rien rapporter.
MIN_LEG_ODDS = 1.20
# Fraction du critere de Kelly appliquee aux mises conseillees. Kelly plein maximise la
# croissance du capital si les probabilites sont exactes ; elles ne le sont jamais, et
# une surestimation ruine le joueur. Le quart de Kelly est l'usage prudent.
KELLY_FRACTION = 0.25


def kelly_share(probability: float, odds: float | None) -> float:
    """Part de capital a miser sur une issue, en fraction de 1, quart de Kelly.

    Vaut 0 des que le pari n'a pas d'esperance positive selon le modele : dans ce cas
    la mise optimale est de ne pas jouer.
    """
    if not odds or odds <= 1:
        return 0.0
    chance = probability / 100
    edge = chance * odds - 1
    if edge <= 0:
        return 0.0
    return KELLY_FRACTION * edge / (odds - 1)


@dataclass
class Leg:
    """Une selection du ticket."""

    match: str
    market: str
    probability: float  # en %
    odds: float | None = None
    kickoff: str | None = None

    @property
    def fair_odds(self) -> float:
        return round(100 / self.probability, 2) if self.probability else 0.0


@dataclass
class Ticket:
    """Un combine et ses caracteristiques financieres."""

    legs: list[Leg] = field(default_factory=list)

    @property
    def probability(self) -> float:
        """Probabilite que les N selections passent toutes, en %.

        Suppose les matchs independants : deux rencontres de la meme competition
        jouees le meme jour ne le sont pas tout a fait, donc ce chiffre est une
        approximation plutot optimiste.
        """
        return round(100 * prod(leg.probability / 100 for leg in self.legs), 2)

    @property
    def fair_odds(self) -> float:
        """Cote en dessous de laquelle le ticket perd de l'argent."""
        probability = self.probability
        return round(100 / probability, 2) if probability else 0.0

    @property
    def odds(self) -> float | None:
        """Cote reellement proposee, si toutes les selections sont cotees."""
        if not self.legs or any(leg.odds is None for leg in self.legs):
            return None
        return round(prod(leg.odds for leg in self.legs if leg.odds), 2)

    @property
    def value(self) -> float | None:
        """Esperance de gain par euro mise, en %, si le modele a raison."""
        odds = self.odds
        if odds is None:
            return None
        return round(100 * (odds * self.probability / 100 - 1), 1)

    @property
    def one_in(self) -> int:
        """Frequence attendue : le ticket sort environ une fois sur N."""
        probability = self.probability
        return round(100 / probability) if probability else 0

    @property
    def deadline(self) -> str | None:
        """Coup d'envoi du premier match : le ticket doit etre valide avant.

        Un combine se joue en une fois ; des que la premiere rencontre demarre, il
        n'est plus pariable tel quel.
        """
        return min((leg.kickoff for leg in self.legs if leg.kickoff), default=None)

    def payout(self, stake: float) -> float | None:
        odds = self.odds
        return round(stake * odds, 2) if odds else None


def _best_selection(bundle: MatchBundle, market: str | None) -> Leg | None:
    """Selection la plus probable d'un match, eventuellement restreinte a un marche."""
    if not bundle.poisson or not bundle.poisson.markets:
        return None

    prices = {
        _SIGN_TO_MARKET.get(sign, sign): value for sign, value in bundle.best_odds().items()
    }
    if market:
        probability = bundle.poisson.markets.get(market)
        if probability is None:
            return None
        return Leg(bundle.label, market, probability, prices.get(market), bundle.stats.kickoff)

    # Sans marche impose, seuls les marches cotes ont un interet, et pas a n'importe
    # quel prix : « plus de 0.5 but » a 1.02 est la selection la plus probable de
    # n'importe quel match, et la moins interessante a jouer.
    priced = {
        name: probability
        for name, probability in bundle.poisson.markets.items()
        if prices.get(name, 0) >= MIN_LEG_ODDS
    }
    name, probability = max((priced or bundle.poisson.markets).items(), key=lambda item: item[1])
    return Leg(bundle.label, name, probability, prices.get(name), bundle.stats.kickoff)


def build_ticket(
    bundles: list[MatchBundle],
    *,
    legs: int = 4,
    market: str | None = None,
    min_probability: float | None = None,
) -> Ticket | None:
    """Assemble le ticket le plus probable a partir des matchs analyses.

    Le produit des probabilites etant maximal quand on prend les selections les
    plus probables, il suffit de trier. Si `min_probability` est fourni et que le
    ticket a `legs` selections passe sous ce seuil, on retire les selections les
    moins probables jusqu'a repasser au-dessus ; s'il n'en reste plus assez, on
    renvoie None plutot qu'un ticket qui ne respecte pas la demande.
    """
    selections = [leg for bundle in bundles if (leg := _best_selection(bundle, market))]
    selections.sort(key=lambda leg: leg.probability, reverse=True)
    if not selections:
        return None

    ticket = Ticket(selections[: max(legs, 1)])
    if min_probability is not None:
        while ticket.legs and ticket.probability < min_probability:
            ticket = Ticket(ticket.legs[:-1])
        if len(ticket.legs) < 2:
            return None

    return _chronological(ticket)


def _chronological(ticket: Ticket) -> Ticket:
    """Selections rangees par coup d'envoi : c'est ainsi qu'on les suit sur le ticket,
    et la premiere donne l'heure limite de validation."""
    ticket.legs.sort(key=lambda leg: (leg.kickoff is None, leg.kickoff or ""))
    return ticket


def _leg_value(leg: Leg) -> float:
    """Esperance de gain par euro mise, en pourcentage, si le modele a raison."""
    return 100 * ((leg.odds or 0) * leg.probability / 100 - 1)


def _priced_selections(
    bundle: MatchBundle, min_probability: float, max_value: float
) -> list[Leg]:
    """Marches cotes du match dont le modele juge la probabilite suffisante.

    Les cotes trop basses sont ecartees : leur esperance est mecaniquement la moins
    mauvaise du marche, ce qui les placerait en tete d'un classement par valeur sans
    qu'elles rapportent quoi que ce soit.
    """
    if not bundle.poisson or not bundle.poisson.markets:
        return []
    prices = {
        _SIGN_TO_MARKET.get(sign, sign): value for sign, value in bundle.best_odds().items()
    }
    legs = [
        Leg(bundle.label, market, probability, odds, bundle.stats.kickoff)
        for market, odds in prices.items()
        if odds >= MIN_LEG_ODDS
        and (probability := bundle.poisson.markets.get(market, 0.0)) >= min_probability
    ]
    return [leg for leg in legs if _leg_value(leg) <= max_value]


def build_value_ticket(
    bundles: list[MatchBundle],
    *,
    legs: int,
    min_leg_probability: float = MIN_LEG_PROBABILITY,
    max_leg_value: float = MAX_LEG_VALUE,
) -> Ticket | None:
    """Combine de `legs` selections cotees maximisant le gain espere.

    Tous les marches sont melanges (double chance, les deux marquent, seuils de buts,
    mi-temps) et une seule selection est prise par match, les issues d'une meme
    rencontre n'etant pas combinables chez le bookmaker. L'esperance d'un combine est
    le produit des `cote x probabilite` de ses selections : la maximiser revient a
    retenir les selections dont ce produit est le plus grand, une fois ecartees celles
    dont le modele juge la probabilite trop faible ou dont l'ecart au marche est trop
    beau pour etre vrai.
    """
    best_per_match: list[Leg] = []
    for bundle in bundles:
        selections = _priced_selections(bundle, min_leg_probability, max_leg_value)
        if selections:
            best_per_match.append(max(selections, key=_leg_value))

    if len(best_per_match) < legs:
        return None
    best_per_match.sort(key=_leg_value, reverse=True)
    return _chronological(Ticket(best_per_match[:legs]))
