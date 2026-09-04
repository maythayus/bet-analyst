"""Construction d'un ticket combine a partir des probabilites du modele.

Les selections retenues sont les plus probables du jour, une par match. La
probabilite d'un combine est le produit des probabilites de ses selections : elle
s'effondre tres vite, et c'est precisement ce que le module rend visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import prod

from betbot import consensus
from betbot.models import MatchBundle
from betbot.poisson import CALIBRATED_SOURCES
from betbot.trap import trap_reasons

# Chez les bookmakers le nul s'ecrit X, dans le modele il s'ecrit N.
_SIGN_TO_MARKET = {"X": "N"}

# Tailles de combines proposees en fin de rapport, en plus du ticket principal.
VALUE_TICKET_SIZES = (6, 8)
BTTS_YES = "Les deux marquent : oui"
BTTS_NO = "Les deux marquent : non"
MAX_TICKET_LABEL = "Combine maximum ({count} selections : tout ce qui passe)"
# Marches ou Forebet publie sa propre probabilite : elle y est reunie a celle du modele
# (voir `betbot.consensus`), et la selection exige `FOREBET_MIN_PROBABILITY` du consensus.
FOREBET_MARKETS = (BTTS_YES, BTTS_NO, "1N", "N2", "12")
# Seuil applique au consensus sur ces marches.
FOREBET_MIN_PROBABILITY = 55.0
SOURCE_FOREBET = consensus.SOURCE_FOREBET
SOURCE_MODEL = consensus.SOURCE_MODEL
SOURCE_CONSENSUS = consensus.SOURCE_CONSENSUS
# Seuil des marches ou le modele decide seul. Une selection a peine au-dessus d'une
# chance sur deux ne vaut pas grand-chose dans un combine long : huit selections a 55 %
# ne passent qu'une fois sur 119.
MIN_LEG_PROBABILITY = 55.0
# Au-dela de cet ecart avec le marche, l'explication la plus probable n'est pas une
# aubaine mais une erreur du modele (equipe mal identifiee, statistiques manquantes) :
# ces selections sont ecartees des combines plutot que recherchees.
MAX_LEG_VALUE = 25.0
# En dessous de cette cote, l'issue est une quasi-certitude que le bookmaker vend a
# perte de temps (« plus de 0.5 but » a 1.03) : elle occupe la premiere place du
# classement par valeur sans rien rapporter.
MIN_LEG_ODDS = 1.20
# Plancher de probabilite d'un combine entier : au moins une chance sur trois. Un ticket
# qui sort une fois sur dix n'est pas montre, meme si chacune de ses selections passe le
# seuil : les combines longs disparaissent d'eux-memes des que le produit descend.
MIN_TICKET_PROBABILITY = 100 / 3
# Fraction du critere de Kelly appliquee aux mises conseillees. Kelly plein maximise la
# croissance du capital si les probabilites sont exactes ; elles ne le sont jamais, et
# une surestimation ruine le joueur. Le quart de Kelly est l'usage prudent.
KELLY_FRACTION = 0.25
# Plafond de mise, en fraction du capital. Kelly reagit violemment a une probabilite
# surestimee : sur une cote basse, deux points d'erreur suffisent a conseiller un
# cinquieme du capital sur un pari perdant.
KELLY_MAX_SHARE = 0.05


def kelly_share(probability: float, odds: float | None) -> float:
    """Part de capital a miser sur une issue, en fraction de 1, quart de Kelly plafonne.

    Vaut 0 des que le pari n'a pas d'esperance positive selon le modele : dans ce cas
    la mise optimale est de ne pas jouer.
    """
    if not odds or odds <= 1:
        return 0.0
    chance = probability / 100
    edge = chance * odds - 1
    if edge <= 0:
        return 0.0
    return min(KELLY_FRACTION * edge / (odds - 1), KELLY_MAX_SHARE)


@dataclass
class Leg:
    """Une selection du ticket."""

    match: str
    market: str
    probability: float  # en %
    odds: float | None = None
    kickoff: str | None = None
    # Origine de la probabilite : Forebet quand il publie le marche, le modele sinon.
    source: str = SOURCE_MODEL

    @property
    def fair_odds(self) -> float:
        return round(100 / self.probability, 2) if self.probability else 0.0


@dataclass
class Ticket:
    """Un combine et ses caracteristiques financieres."""

    legs: list[Leg] = field(default_factory=list)
    # Titre du ticket dans le rapport, quand le nombre de selections ne le decrit pas.
    label: str | None = None

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


def _leg_for(
    bundle: MatchBundle, market: str, odds: float | None, min_probability: float
) -> Leg | None:
    """Selection d'un marche, ecartee si elle est trop peu probable ou piegeuse.

    Sur les marches que Forebet publie (les deux marquent oui/non, doubles chances), sa
    probabilite est reunie a celle du modele en une seule valeur, qui doit atteindre
    `FOREBET_MIN_PROBABILITY`. Les deux sources doivent aussi se rejoindre : quand elles
    se contredisent, la cote du bookmaker departage celle qui s'en approche le plus, et a
    defaut rien n'est retenu (voir `betbot.consensus`). Ailleurs le modele reste seul, au
    seuil habituel.

    Une selection designee comme piege par `betbot.trap` est refusee quelle que soit sa
    probabilite : classement serre, defenses trop solides ou trop friables, score
    pronostique ferme, ou confrontations directes qui racontent l'inverse.
    """
    if market in FOREBET_MARKETS:
        agreed = consensus.for_market(bundle, market)
        if agreed is None:
            return None
        probability, source = agreed.probability, agreed.source
        # Le seuil Forebet porte sur ce qu'il dit lui-meme : quand il ne publie pas le
        # marche, le modele reste juge au seuil habituel.
        forebet_spoke = source != SOURCE_MODEL
        floor = max(min_probability, FOREBET_MIN_PROBABILITY) if forebet_spoke else min_probability
    else:
        model = bundle.poisson.markets.get(market) if bundle.poisson else None
        if model is None:
            return None
        probability, source, floor = model, SOURCE_MODEL, min_probability

    if probability < floor or trap_reasons(bundle.stats, market, bundle.predicted_score):
        return None
    return Leg(bundle.label, market, probability, odds, bundle.stats.kickoff, source)


def _best_selection(bundle: MatchBundle, market: str | None) -> Leg | None:
    """Selection la plus probable d'un match, eventuellement restreinte a un marche."""
    if not bundle.poisson or not bundle.poisson.markets:
        return None

    prices = bundle.market_prices()
    if market:
        return _leg_for(bundle, market, prices.get(market), 0.0)

    # Sans marche impose, seuls les marches cotes ont un interet, et pas a n'importe
    # quel prix : « plus de 0.5 but » a 1.02 est la selection la plus probable de
    # n'importe quel match, et la moins interessante a jouer.
    candidates = [
        leg
        for name in bundle.poisson.markets
        if (leg := _leg_for(bundle, name, prices.get(name), 0.0))
    ]
    priced = [leg for leg in candidates if (leg.odds or 0) >= MIN_LEG_ODDS]
    kept = priced or candidates
    return max(kept, key=_leg_probability) if kept else None


def build_ticket(
    bundles: list[MatchBundle],
    *,
    legs: int = 4,
    market: str | None = None,
    min_probability: float | None = None,
) -> Ticket | None:
    """Assemble le ticket le plus probable a partir des matchs analyses.

    Le produit des probabilites etant maximal quand on prend les selections les
    plus probables, il suffit de trier. Si le ticket a `legs` selections passe sous
    `min_probability` (par defaut `MIN_TICKET_PROBABILITY`, une chance sur trois), on
    retire les selections les moins probables jusqu'a repasser au-dessus ; s'il n'en
    reste plus assez, on renvoie None plutot qu'un ticket qui ne respecte pas la demande.
    """
    selections = [leg for bundle in bundles if (leg := _best_selection(bundle, market))]
    selections.sort(key=lambda leg: leg.probability, reverse=True)
    if not selections:
        return None

    floor = MIN_TICKET_PROBABILITY if min_probability is None else min_probability
    ticket = Ticket(selections[: max(legs, 1)])
    while ticket.legs and ticket.probability < floor:
        ticket = Ticket(ticket.legs[:-1])
    if not ticket.legs or (len(ticket.legs) < 2 and len(ticket.legs) < legs):
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


def _leg_probability(leg: Leg) -> float:
    return leg.probability


def market_calibrated(bundles: list[MatchBundle]) -> bool:
    """Vrai si les probabilites viennent d'un modele cale sur les cotes.

    Le modele de forme est systematiquement plus tranche que le marche : classer ses
    selections par valeur revient a choisir celles ou il s'ecarte le plus du
    bookmaker, c'est-a-dire celles ou il a le plus de chances de se tromper. Ses
    combines sont donc construits par probabilite decroissante, comme dans les
    premieres versions de Bet.Bot.
    """
    sources = [bundle.poisson.source for bundle in bundles if bundle.poisson]
    return bool(sources) and all(source in CALIBRATED_SOURCES for source in sources)


def _priced_selections(
    bundle: MatchBundle, min_probability: float, max_value: float | None
) -> list[Leg]:
    """Marches cotes du match dont le modele juge la probabilite suffisante.

    Les cotes trop basses sont ecartees : leur esperance est mecaniquement la moins
    mauvaise du marche, ce qui les placerait en tete d'un classement par valeur sans
    qu'elles rapportent quoi que ce soit.
    """
    if not bundle.poisson or not bundle.poisson.markets:
        return []
    legs = [
        leg
        for market, odds in bundle.market_prices().items()
        if odds >= MIN_LEG_ODDS and (leg := _leg_for(bundle, market, odds, min_probability))
    ]
    if max_value is None:
        return legs
    return [leg for leg in legs if _leg_value(leg) <= max_value]


def _ranked_selections(
    bundles: list[MatchBundle], min_leg_probability: float, max_leg_value: float
) -> list[Leg]:
    """Meilleure selection cotee de chaque match, de la plus interessante a la moins.

    Le classement suit `market_calibrated` : par esperance quand le modele est cale sur
    les cotes, par probabilite sinon (et sans plafond de valeur, l'ecart au marche etant
    alors la regle).
    """
    calibrated = market_calibrated(bundles)
    rank = _leg_value if calibrated else _leg_probability
    cap = max_leg_value if calibrated else None

    best_per_match: list[Leg] = []
    for bundle in bundles:
        selections = _priced_selections(bundle, min_leg_probability, cap)
        if selections:
            best_per_match.append(max(selections, key=rank))
    best_per_match.sort(key=rank, reverse=True)
    return best_per_match


def build_max_ticket(
    bundles: list[MatchBundle],
    *,
    min_leg_probability: float = MIN_LEG_PROBABILITY,
    max_leg_value: float = MAX_LEG_VALUE,
) -> Ticket | None:
    """Combine qui empile toutes les selections valides du jour, une par match.

    Aucune limite de taille : tant qu'une rencontre offre une selection au-dessus du
    seuil, cotee au moins `MIN_LEG_ODDS` et non piegeuse, elle entre. Le gain affiche
    grossit avec chaque match, et la probabilite fond d'autant : douze selections a
    60 % ne passent qu'une fois sur 460. Sous deux selections, ce n'est pas un combine,
    et sous `MIN_TICKET_PROBABILITY` il n'est pas propose.
    """
    legs = _ranked_selections(bundles, min_leg_probability, max_leg_value)
    if len(legs) < 2:
        return None
    ticket = Ticket(legs, label=MAX_TICKET_LABEL.format(count=len(legs)))
    if ticket.probability < MIN_TICKET_PROBABILITY:
        return None
    return _chronological(ticket)


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

    Avec le modele de forme, dont l'ecart au marche est la regle et non l'exception, ce
    tri par esperance selectionnerait les erreurs du modele : les selections sont alors
    classees par probabilite decroissante, et le plafond de valeur ne s'applique pas.

    Quel que soit le classement, un ticket dont la probabilite globale passe sous
    `MIN_TICKET_PROBABILITY` n'est pas propose.
    """
    best_per_match = _ranked_selections(bundles, min_leg_probability, max_leg_value)
    if len(best_per_match) < legs:
        return None
    ticket = Ticket(best_per_match[:legs])
    if ticket.probability < MIN_TICKET_PROBABILITY:
        return None
    return _chronological(ticket)
