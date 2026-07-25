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

    def implied_probabilities(self) -> dict[str, float] | None:
        """Probabilites implicites des cotes, corrigees de la marge du bookmaker."""
        keys = ("1", "X", "2")
        if not all(self.odds.get(k) for k in keys):
            return None
        raw = {k: 1.0 / self.odds[k] for k in keys}
        overround = sum(raw.values())
        return {k: round(100 * v / overround, 2) for k, v in raw.items()}


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


@dataclass
class MatchBundle:
    """Tout ce que l'on sait d'un match, pret a etre envoye au LLM."""

    stats: MatchStats
    forebet: ForebetPrediction | None = None
    poisson: PoissonResult | None = None

    @property
    def label(self) -> str:
        return f"{self.stats.home_team} vs {self.stats.away_team}"

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
        }


@dataclass
class Analysis:
    """Verdict produit par le LLM pour un match."""

    match: str
    markdown: str
    raw: str
    model: str
