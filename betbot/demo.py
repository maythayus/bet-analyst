"""Jeu de donnees fictif pour tester le pipeline sans reseau (`--demo`)."""

from __future__ import annotations

from betbot.models import ForebetPrediction, MatchStats, TableStanding, TeamForm

_FIXTURES: list[tuple[MatchStats, ForebetPrediction]] = [
    (
        MatchStats(
            home_team="Olympique Lyonnais",
            away_team="Stade Rennais",
            kickoff="2026-07-26 21:00",
            competition="Ligue 1",
            home_form=TeamForm(
                name="Olympique Lyonnais",
                last_results=["W", "W", "D", "L", "W"],
                goals_for=9,
                goals_against=5,
                matches_played=5,
            ),
            away_form=TeamForm(
                name="Stade Rennais",
                last_results=["L", "D", "W", "L", "D"],
                goals_for=5,
                goals_against=7,
                matches_played=5,
            ),
            head_to_head=[
                "2026-03-02 Stade Rennais 1-2 Olympique Lyonnais",
                "2025-10-19 Olympique Lyonnais 3-1 Stade Rennais",
                "2025-04-06 Stade Rennais 2-2 Olympique Lyonnais",
            ],
            home_table=TableStanding(
                name="Olympique Lyonnais",
                position=4,
                played=30,
                wins=15,
                draws=8,
                goals_for=48,
                goals_against=33,
                points=53,
            ),
            away_table=TableStanding(
                name="Stade Rennais",
                position=9,
                played=30,
                wins=11,
                draws=9,
                goals_for=39,
                goals_against=40,
                points=42,
            ),
        ),
        ForebetPrediction(
            home_team="Olympique Lyonnais",
            away_team="Stade Rennais",
            kickoff="2026-07-26 21:00",
            competition="Ligue 1",
            prob_home=52.0,
            prob_draw=26.0,
            prob_away=22.0,
            predicted_score="2-1",
            avg_goals=2.8,
            odds={"1": 1.85, "X": 3.60, "2": 4.20},
        ),
    ),
    (
        MatchStats(
            home_team="Getafe",
            away_team="Athletic Bilbao",
            kickoff="2026-07-26 19:00",
            competition="LaLiga",
            home_form=TeamForm(
                name="Getafe",
                last_results=["D", "L", "D", "L", "W"],
                goals_for=4,
                goals_against=6,
                matches_played=5,
            ),
            away_form=TeamForm(
                name="Athletic Bilbao",
                last_results=["W", "W", "W", "D", "W"],
                goals_for=11,
                goals_against=3,
                matches_played=5,
            ),
            head_to_head=[
                "2026-02-15 Athletic Bilbao 2-0 Getafe",
                "2025-09-28 Getafe 0-0 Athletic Bilbao",
            ],
            home_table=TableStanding(
                name="Getafe",
                position=14,
                played=30,
                wins=8,
                draws=10,
                goals_for=27,
                goals_against=38,
                points=34,
            ),
            away_table=TableStanding(
                name="Athletic Bilbao",
                position=3,
                played=30,
                wins=17,
                draws=7,
                goals_for=52,
                goals_against=24,
                points=58,
            ),
        ),
        ForebetPrediction(
            home_team="Getafe",
            away_team="Athletic Bilbao",
            kickoff="2026-07-26 19:00",
            competition="LaLiga",
            prob_home=22.0,
            prob_draw=30.0,
            prob_away=48.0,
            predicted_score="0-1",
            avg_goals=2.1,
            odds={"1": 4.00, "X": 3.30, "2": 1.95},
        ),
    ),
]


def predictions() -> list[ForebetPrediction]:
    return [prediction for _, prediction in _FIXTURES]


def stats_for(home_team: str, away_team: str) -> MatchStats | None:
    for stats, _ in _FIXTURES:
        if stats.home_team == home_team and stats.away_team == away_team:
            return stats
    return None
