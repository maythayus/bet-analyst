"""Structures de donnees partagees par les scrapers, le modele et le LLM."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PlayedMatch:
    """Un match deja joue, vu du cote de l'une des deux equipes.

    Garder le detail match par match, et pas seulement des moyennes, est ce qui permet
    de ponderer les rencontres recentes, de separer domicile et exterieur et de tenir
    compte du niveau de l'adversaire.
    """

    opponent: str
    scored: int
    conceded: int
    at_home: bool
    date: str = ""

    @property
    def result(self) -> str:
        return "W" if self.scored > self.conceded else "D" if self.scored == self.conceded else "L"


@dataclass
class TeamForm:
    """Forme recente d'une equipe, telle qu'extraite de Flashscore."""

    name: str
    last_results: list[str] = field(default_factory=list)  # ex. ["W", "D", "L", "W", "W"]
    goals_for: int = 0
    goals_against: int = 0
    matches_played: int = 0
    # Matchs retenus, du plus recent au plus ancien. Vide quand seules les moyennes sont
    # connues : le modele retombe alors sur elles.
    matches: list[PlayedMatch] = field(default_factory=list)

    @property
    def avg_goals_for(self) -> float:
        return self.goals_for / self.matches_played if self.matches_played else 0.0

    @property
    def avg_goals_against(self) -> float:
        return self.goals_against / self.matches_played if self.matches_played else 0.0

    @property
    def points_per_game(self) -> float:
        if not self.last_results:
            return 0.0
        pts = sum({"W": 3, "D": 1, "L": 0}.get(r, 0) for r in self.last_results)
        return pts / len(self.last_results)


@dataclass
class ForebetPrediction:
    """Prediction mathematique publiee par Forebet pour une rencontre."""

    home_team: str
    away_team: str
    kickoff: str | None = None
    competition: str | None = None
    prob_home: float | None = None  # en %
    prob_draw: float | None = None
    prob_away: float | None = None
    predicted_score: str | None = None
    avg_goals: float | None = None
    odds: dict[str, float] = field(default_factory=dict)
    url: str | None = None
    # Probabilites publiees par les pages Forebet specialisees (les deux equipes
    # marquent, plus/moins de buts, double chance, mi-temps), nommees comme les
    # marches du modele quand l'equivalent existe. "Plus de 2.5 buts" -> 61.0
    markets: dict[str, float] = field(default_factory=dict)

    def implied_probabilities(self) -> dict[str, float] | None:
        """Probabilites implicites des cotes, corrigees de la marge du bookmaker."""
        return implied_from_odds(self.odds)

    @property
    def best_probability(self) -> float | None:
        """Probabilite du pronostic le plus probable selon Forebet."""
        values = [p for p in (self.prob_home, self.prob_draw, self.prob_away) if p is not None]
        return max(values) if values else None

    @property
    def pick(self) -> str | None:
        """Signe correspondant a la probabilite la plus elevee : 1, X ou 2."""
        pairs = [
            (sign, prob)
            for sign, prob in (("1", self.prob_home), ("X", self.prob_draw), ("2", self.prob_away))
            if prob is not None
        ]
        return max(pairs, key=lambda pair: pair[1])[0] if pairs else None


def implied_from_odds(odds: dict[str, float]) -> dict[str, float] | None:
    """Convertit des cotes 1X2 en probabilites, marge du bookmaker retiree."""
    keys = ("1", "X", "2")
    if not all(odds.get(key) for key in keys):
        return None
    raw = {key: 1.0 / odds[key] for key in keys}
    overround = sum(raw.values())
    return {key: round(100 * value / overround, 2) for key, value in raw.items()}


@dataclass
class TableStanding:
    """Ligne de classement d'une equipe, lue sur Flashscore.

    Porte sur toute la saison, contrairement a `TeamForm` qui ne regarde que les cinq
    derniers matchs : c'est la mesure la plus stable de la solidite d'une defense.
    """

    name: str
    position: int
    played: int = 0
    wins: int = 0
    draws: int = 0
    goals_for: int = 0
    goals_against: int = 0
    points: int = 0

    @property
    def conceded_per_game(self) -> float | None:
        return self.goals_against / self.played if self.played else None

    @property
    def scored_per_game(self) -> float | None:
        return self.goals_for / self.played if self.played else None

    @property
    def draw_share(self) -> float | None:
        """Part de matchs nuls, en fraction de 1."""
        return self.draws / self.played if self.played else None


@dataclass
class MatchStats:
    """Statistiques brutes d'une rencontre collectees sur Flashscore."""

    home_team: str
    away_team: str
    kickoff: str | None = None
    competition: str | None = None
    # Pays ou se joue la rencontre, tel que Flashscore classe la competition.
    country: str | None = None
    home_form: TeamForm | None = None
    away_form: TeamForm | None = None
    head_to_head: list[str] = field(default_factory=list)
    home_table: TableStanding | None = None
    away_table: TableStanding | None = None
    # Classement complet de la competition : il donne le niveau des adversaires
    # rencontres, sans quoi trois buts contre le dernier valent trois buts contre le
    # leader.
    standings: list[TableStanding] = field(default_factory=list)
    url: str | None = None

    @property
    def table_gap(self) -> int | None:
        """Nombre de places separant les deux equipes au classement."""
        if not self.home_table or not self.away_table:
            return None
        return abs(self.home_table.position - self.away_table.position)


@dataclass
class PoissonResult:
    """Sortie du modele statistique."""

    prob_home: float
    prob_draw: float
    prob_away: float
    prob_over_25: float
    prob_btts: float
    expected_home_goals: float
    expected_away_goals: float
    most_likely_score: str
    markets: dict[str, float] = field(default_factory=dict)  # "1N et oui" -> probabilite en %
    # Origine des buts attendus : "cotes", "cotes + forme" ou "forme seule". Une
    # estimation issue de la forme seule porte sur cinq matchs sans tenir compte du
    # niveau des adversaires : elle est nettement moins fiable.
    source: str = "forme seule"
    # Ecart maximal, en points, entre le modele et les marches sur lesquels il a ete
    # cale. Vaut None quand aucune cote n'a servi de reference.
    calibration_gap: float | None = None


@dataclass
class BookmakerLine:
    """Cotes 1X2 d'un bookmaker pour la rencontre."""

    bookmaker: str
    odds: dict[str, float] = field(default_factory=dict)

    def implied_probabilities(self) -> dict[str, float] | None:
        return implied_from_odds(self.odds)


# Ce que le prompt du LLM garde de la forme et des confrontations directes. Le modele,
# lui, continue de travailler sur la totalite.
PROMPT_MATCHES = 6
PROMPT_HEAD_TO_HEAD = 5


def _form_summary(form: TeamForm | None) -> dict[str, Any] | None:
    """Forme d'une equipe en quelques lignes plutot qu'en vingt objets."""
    if form is None:
        return None
    recent = [
        f"{played.date} {'dom' if played.at_home else 'ext'} vs {played.opponent} "
        f"{played.scored}-{played.conceded}"
        for played in form.matches[:PROMPT_MATCHES]
    ]
    return {
        "name": form.name,
        "last_results": form.last_results,
        "matches_played": form.matches_played,
        "avg_goals_for": round(form.avg_goals_for, 2),
        "avg_goals_against": round(form.avg_goals_against, 2),
        "recent_matches": recent,
    }


@dataclass
class MatchBundle:
    """Tout ce que l'on sait d'un match, pret a etre envoye au LLM."""

    stats: MatchStats
    forebet: ForebetPrediction | None = None
    poisson: PoissonResult | None = None
    bookmakers: list[BookmakerLine] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.stats.home_team} vs {self.stats.away_team}"

    @property
    def predicted_score(self) -> str | None:
        """Score pronostique par Forebet, quand sa page en publie un."""
        return self.forebet.predicted_score if self.forebet else None

    def best_odds(self) -> dict[str, float]:
        """Meilleure cote disponible pour chaque signe, tous bookmakers confondus."""
        best: dict[str, float] = {}
        for line in self.bookmakers:
            for sign, value in line.odds.items():
                if value > best.get(sign, 0):
                    best[sign] = value
        return best

    def market_prices(self) -> dict[str, float]:
        """Meilleures cotes, nommees comme les marches du modele (le nul s'ecrit N)."""
        prices: dict[str, float] = {}
        for sign, odds in self.best_odds().items():
            market = "N" if sign == "X" else sign
            prices[market] = max(odds, prices.get(market, 0))
        return prices

    def opportunities(
        self, odds_range: tuple[float, float] | None = None
    ) -> list[tuple[str, float, float, float]]:
        """Marches cotes, tries par valeur decroissante.

        Retourne des tuples `(marche, cote, probabilite du modele, valeur en %)`. La
        valeur est l'esperance de gain par euro mise, `cote * probabilite - 1` : elle
        n'est positive que si le modele juge l'issue plus probable que le marche.
        """
        if not self.poisson or not self.poisson.markets:
            return []
        prices = self.market_prices()
        found: list[tuple[str, float, float, float]] = []
        for market, odds in prices.items():
            probability = self.poisson.markets.get(market)
            if probability is None:
                continue
            if odds_range and not odds_range[0] <= odds <= odds_range[1]:
                continue
            value = round(100 * (odds * probability / 100 - 1), 1)
            found.append((market, odds, probability, value))
        return sorted(found, key=lambda item: item[3], reverse=True)

    def value_gap(self) -> dict[str, float] | None:
        """Ecart, en points, entre la probabilite Poisson et celle implicite des cotes.

        Un ecart positif signifie que le modele juge l'issue plus probable que le
        marche : c'est la seule situation ou une mise a une esperance positive, sous
        reserve que le modele soit juste.
        """
        implied = implied_from_odds(self.best_odds())
        if not implied or not self.poisson:
            return None
        model = {
            "1": self.poisson.prob_home,
            "X": self.poisson.prob_draw,
            "2": self.poisson.prob_away,
        }
        return {sign: round(model[sign] - implied[sign], 2) for sign in ("1", "X", "2")}

    def to_prompt_dict(self) -> dict[str, Any]:
        """Version resserree de `to_dict()`, destinee au LLM.

        Le classement complet de la competition et le detail des vingt matchs servent au
        modele, pas au commentaire : les envoyer depassait la fenetre de contexte, et le
        serveur refusait alors la rencontre entiere. Ne restent que les elements dont le
        commentaire a besoin, les chiffres eux-memes etant deja calcules.
        """
        return {
            "match": self.label,
            "kickoff": self.stats.kickoff,
            "competition": self.stats.competition,
            "country": self.stats.country,
            "flashscore": {
                "home_form": _form_summary(self.stats.home_form),
                "away_form": _form_summary(self.stats.away_form),
                "home_table": asdict(self.stats.home_table) if self.stats.home_table else None,
                "away_table": asdict(self.stats.away_table) if self.stats.away_table else None,
                "head_to_head": self.stats.head_to_head[:PROMPT_HEAD_TO_HEAD],
            },
            "forebet": (
                {
                    "prob_home": self.forebet.prob_home,
                    "prob_draw": self.forebet.prob_draw,
                    "prob_away": self.forebet.prob_away,
                    "predicted_score": self.forebet.predicted_score,
                    "avg_goals": self.forebet.avg_goals,
                    "markets": self.forebet.markets,
                }
                if self.forebet
                else None
            ),
            "poisson": asdict(self.poisson) if self.poisson else None,
            "best_odds": self.best_odds() or None,
            "market_odds": self.market_prices() or None,
            "implied_from_best_odds": implied_from_odds(self.best_odds()),
            "value_gap_poisson_vs_market": self.value_gap(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "match": self.label,
            "kickoff": self.stats.kickoff,
            "competition": self.stats.competition,
            "country": self.stats.country,
            "flashscore": asdict(self.stats),
            "forebet": asdict(self.forebet) if self.forebet else None,
            "forebet_implied_from_odds": (
                self.forebet.implied_probabilities() if self.forebet else None
            ),
            "poisson": asdict(self.poisson) if self.poisson else None,
            "bookmakers": [asdict(line) for line in self.bookmakers],
            "best_odds": self.best_odds() or None,
            "implied_from_best_odds": implied_from_odds(self.best_odds()),
            "value_gap_poisson_vs_market": self.value_gap(),
        }


@dataclass
class Analysis:
    """Verdict produit par le LLM pour un match."""

    match: str
    markdown: str
    raw: str
    model: str
