"""Tests hors ligne : `python -m unittest discover -s tests`."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from betanalyst import demo, poisson
from betanalyst.config import AppConfig
from betanalyst.models import ForebetPrediction
from betanalyst.pipeline import build_bundles
from betanalyst.report import build_markdown
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
