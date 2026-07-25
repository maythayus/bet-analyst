"""Tests hors ligne : `python -m unittest discover -s tests`."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from betanalyst import demo, poisson
from betanalyst.config import AppConfig
from betanalyst.models import BookmakerLine, ForebetPrediction, MatchBundle
from betanalyst.pipeline import build_bundles, filter_predictions
from betanalyst.report import build_markdown
from betanalyst.sources import bookmakers
from betanalyst.sources.forebet import parse_predictions

SAMPLE_HTML = """
<div class="rcnt">
  <span class="shortTag">FR1</span>
  <span class="date_bah">26/07/2026 21:00</span>
  <span class="homeTeam"><span>Lyon</span></span>
  <span class="awayTeam"><span>Rennes</span></span>
  <div class="fprc"><span>52</span><span>26</span><span>22</span></div>
  <div class="ex_sc">2-1</div>
  <div class="avg_sc">2.8</div>
  <a href="/en/football-tips/lyon-rennes">details</a>
</div>
"""


class TestForebetParsing(unittest.TestCase):
    def test_parses_probabilities_and_teams(self) -> None:
        (prediction,) = parse_predictions(SAMPLE_HTML)
        self.assertEqual(prediction.home_team, "Lyon")
        self.assertEqual(prediction.away_team, "Rennes")
        self.assertEqual(
            (prediction.prob_home, prediction.prob_draw, prediction.prob_away), (52.0, 26.0, 22.0)
        )
        self.assertEqual(prediction.predicted_score, "2-1")
        self.assertTrue(prediction.url.endswith("/lyon-rennes"))

    def test_ignores_rows_without_teams(self) -> None:
        self.assertEqual(parse_predictions('<div class="rcnt"><span>x</span></div>'), [])


class TestImpliedProbabilities(unittest.TestCase):
    def test_removes_bookmaker_margin(self) -> None:
        prediction = ForebetPrediction(
            home_team="A", away_team="B", odds={"1": 2.0, "X": 4.0, "2": 4.0}
        )
        implied = prediction.implied_probabilities()
        self.assertAlmostEqual(sum(implied.values()), 100.0, places=1)
        self.assertGreater(implied["1"], implied["X"])

    def test_returns_none_without_full_odds(self) -> None:
        prediction = ForebetPrediction(home_team="A", away_team="B", odds={"1": 2.0})
        self.assertIsNone(prediction.implied_probabilities())


class TestPoisson(unittest.TestCase):
    def setUp(self) -> None:
        self.stats = demo.stats_for("Olympique Lyonnais", "Stade Rennais")

    def test_probabilities_sum_to_one(self) -> None:
        result = poisson.compute(self.stats)
        total = result.prob_home + result.prob_draw + result.prob_away
        self.assertAlmostEqual(total, 100.0, delta=0.5)

    def test_favours_the_stronger_home_side(self) -> None:
        result = poisson.compute(self.stats)
        self.assertGreater(result.prob_home, result.prob_away)

    def test_returns_none_without_form(self) -> None:
        stats = demo.stats_for("Getafe", "Athletic Bilbao")
        stats_without_form = type(stats)(home_team=stats.home_team, away_team=stats.away_team)
        self.assertIsNone(poisson.compute(stats_without_form))


class TestCombinedMarkets(unittest.TestCase):
    def setUp(self) -> None:
        stats = demo.stats_for("Olympique Lyonnais", "Stade Rennais")
        self.markets = poisson.compute(stats).markets

    def test_double_chance_is_the_sum_of_its_parts(self) -> None:
        self.assertAlmostEqual(
            self.markets["1N"], self.markets["1"] + self.markets["N"], delta=0.05
        )
        self.assertAlmostEqual(
            self.markets["12"], self.markets["1"] + self.markets["2"], delta=0.05
        )

    def test_btts_yes_and_no_are_complementary(self) -> None:
        total = self.markets["Les deux marquent : oui"] + self.markets["Les deux marquent : non"]
        self.assertAlmostEqual(total, 100.0, delta=0.5)

    def test_combined_market_is_narrower_than_its_components(self) -> None:
        self.assertLess(self.markets["1N et oui"], self.markets["1N"])
        self.assertLess(self.markets["1N et oui"], self.markets["Les deux marquent : oui"])


UNIBET_PAGE = {
    "items": {
        "e1": {"a": "Lyon", "b": "Rennes", "pdesc": "Ligue 1", "start": "2607252100"},
        "m9": {"parent": "e1", "style": "WIN_DRAW_WIN", "desc": "1 N 2"},
        "o1": {"parent": "m9", "desc": "Lyon", "price": "1,80", "pos": 1},
        "o2": {"parent": "m9", "desc": "N", "price": "3,60", "pos": 2},
        "o3": {"parent": "m9", "desc": "Rennes", "price": "4,20", "pos": 3},
    }
}


class TestBookmakers(unittest.TestCase):
    def test_parses_prices_and_signs(self) -> None:
        (entry,) = bookmakers._parse_unibet_page(UNIBET_PAGE)
        self.assertTrue(entry.complete)
        self.assertEqual(entry.odds, {"1": 1.80, "X": 3.60, "2": 4.20})
        self.assertEqual(entry.competition, "Ligue 1")

    def test_matches_teams_despite_naming_differences(self) -> None:
        self.assertTrue(bookmakers.teams_match("Olympique Lyonnais", "Lyonnais"))
        self.assertTrue(bookmakers.teams_match("FC Bohemians 1905", "Bohemians 1905"))
        self.assertTrue(bookmakers.teams_match("Stade Rennais", "Rennes"))
        self.assertFalse(bookmakers.teams_match("Lyon", "Rennes"))
        self.assertFalse(bookmakers.teams_match("Manchester City", "Manchester United"))

    def test_find_uses_both_teams(self) -> None:
        entries = bookmakers._parse_unibet_page(UNIBET_PAGE)
        self.assertIsNotNone(bookmakers.find(entries, "Lyon", "Stade Rennais"))
        self.assertIsNone(bookmakers.find(entries, "Lyon", "Monaco"))


class TestOddsFiltering(unittest.TestCase):
    def setUp(self) -> None:
        self.prediction = ForebetPrediction(
            home_team="Lyon", away_team="Rennes", prob_home=96.0, prob_draw=2.0, prob_away=2.0
        )
        self.entries = [
            bookmakers.BookmakerOdds(
                bookmaker="Unibet",
                home_team="Lyon",
                away_team="Rennes",
                odds={"1": 1.20, "X": 6.0, "2": 12.0},
            )
        ]

    def test_keeps_high_probability_pick(self) -> None:
        kept = filter_predictions(
            [self.prediction],
            self.entries,
            only_bettable=True,
            min_probability=95,
            min_odds=None,
        )
        self.assertEqual(len(kept), 1)

    def test_drops_pick_below_minimum_odds(self) -> None:
        kept = filter_predictions(
            [self.prediction], self.entries, only_bettable=True, min_probability=95, min_odds=1.5
        )
        self.assertEqual(kept, [])

    def test_keeps_only_matches_priced_in_range(self) -> None:
        kept = filter_predictions(
            [self.prediction],
            self.entries,
            only_bettable=True,
            min_probability=None,
            min_odds=None,
            odds_range=(1.65, 1.95),
        )
        self.assertEqual(kept, [])

        self.entries[0].odds["1"] = 1.80
        kept = filter_predictions(
            [self.prediction],
            self.entries,
            only_bettable=True,
            min_probability=None,
            min_odds=None,
            odds_range=(1.65, 1.95),
        )
        self.assertEqual(len(kept), 1)

    def test_drops_match_absent_from_bookmakers(self) -> None:
        kept = filter_predictions(
            [ForebetPrediction(home_team="Brest", away_team="Nice", prob_home=99.0)],
            self.entries,
            only_bettable=True,
            min_probability=None,
            min_odds=None,
        )
        self.assertEqual(kept, [])


class TestOpportunities(unittest.TestCase):
    def _bundle(self) -> MatchBundle:
        stats = demo.stats_for("Olympique Lyonnais", "Stade Rennais")
        return MatchBundle(
            stats=stats,
            poisson=poisson.compute(stats),
            bookmakers=[BookmakerLine(bookmaker="Unibet", odds={"1": 1.80, "X": 3.60, "2": 4.20})],
        )

    def test_value_is_positive_when_model_beats_the_price(self) -> None:
        bundle = self._bundle()
        market, odds, probability, value = bundle.opportunities()[0]
        self.assertEqual(market, "1")
        self.assertAlmostEqual(value, 100 * (odds * probability / 100 - 1), places=1)
        self.assertGreater(value, 0)

    def test_range_excludes_prices_outside_the_window(self) -> None:
        markets = {item[0] for item in self._bundle().opportunities((1.65, 1.95))}
        self.assertEqual(markets, {"1"})


class TestPipelineOffline(unittest.TestCase):
    def test_builds_bundles_and_report_without_network(self) -> None:
        bundles = build_bundles(demo.predictions(), AppConfig(), offline=True)
        self.assertEqual(len(bundles), 2)
        self.assertTrue(all(bundle.poisson for bundle in bundles))

        markdown = build_markdown([(bundle, None) for bundle in bundles])
        self.assertIn("Olympique Lyonnais vs Stade Rennais", markdown)
        self.assertIn("| Poisson |", markdown)


if __name__ == "__main__":
    unittest.main()
