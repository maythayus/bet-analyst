"""Construction d'un ticket combine a partir des probabilites du modele.

Les selections retenues sont les plus probables du jour, une par match. La
probabilite d'un combine est le produit des probabilites de ses selections : elle
s'effondre tres vite, et c'est precisement ce que le module rend visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import prod

from betanalyst.models import MatchBundle

# Chez les bookmakers le nul s'ecrit X, dans le modele il s'ecrit N.
_SIGN_TO_MARKET = {"X": "N"}


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
    candidates = bundle.poisson.markets
    if market:
        probability = candidates.get(market)
        if probability is None:
            return None
        return Leg(bundle.label, market, probability, prices.get(market), bundle.stats.kickoff)

    name, probability = max(candidates.items(), key=lambda item: item[1])
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

    # Les selections sont choisies par probabilite, puis affichees dans l'ordre des
    # coups d'envoi : c'est ainsi qu'on les suit, et le premier donne l'heure limite.
    ticket.legs.sort(key=lambda leg: (leg.kickoff is None, leg.kickoff or ""))
    return ticket
