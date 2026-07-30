"""Structures de donnees partagees par les scrapers, le modele et le LLM."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TeamForm:
    """Forme recente d'une equipe, telle qu'extraite de Flashscore."""

    name: str
    last_results: list[str] = field(default_factory=list)  # ex. ["W", "D", "L", "W", "W"]
    goals_for: int = 0
    goals_against: int = 0
    matches_played: int = 0
    home_goals_for: float | None = None
    home_goals_against: float | None = None
    away_goals_for: float | None = None
    away_goals_against: float | None = None

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
class MatchStats:
    """Statistiques brutes d'une rencontre collectees sur Flashscore."""

    home_team: str
    away_team: str
    kickoff: str | None = None
    competition: str | None = None
    home_form: TeamForm | None = None
    away_form: TeamForm | None = None
    head_to_head: list[str] = field(default_factory=list)
    home_table_position: int | None = None
    away_table_position: int | None = None
    url: str | None = None


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


@dataclass
class BookmakerLine:
    """Cotes 1X2 d'un bookmaker pour la rencontre."""

    bookmaker: str
    odds: dict[str, float] = field(default_factory=dict)

    def implied_probabilities(self) -> dict[str, float] | None:
        return implied_from_odds(self.odds)


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

    def best_odds(self) -> dict[str, float]:
        """Meilleure cote disponible pour chaque signe, tous bookmakers confondus."""
        best: dict[str, float] = {}
        for line in self.bookmakers:
            for sign, value in line.odds.items():
                if value > best.get(sign, 0):
                    best[sign] = value
        return best

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
        prices: dict[str, float] = {}
        for sign, odds in self.best_odds().items():
            market = "N" if sign == "X" else sign  # le nul s'ecrit X chez les bookmakers
            prices[market] = max(odds, prices.get(market, 0))

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "match": self.label,
            "kickoff": self.stats.kickoff,
            "competition": self.stats.competition,
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
