"""Tests hors ligne : `python -m unittest discover -s tests`."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from math import exp, factorial
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from betbot import demo, poisson
from betbot.cli import discover_market_pages, open_report
from betbot.combo import KELLY_MAX_SHARE, build_ticket, build_value_ticket, kelly_share
from betbot.config import AppConfig, MailConfig, ScrapeConfig, WordPressConfig
from betbot.models import (
    BookmakerLine,
    ForebetPrediction,
    MatchBundle,
    MatchStats,
    TeamForm,
)
from betbot.pipeline import (
    build_bundles,
    filter_predictions,
    merge_forebet_markets,
    predictions_from_odds,
)
from betbot.report import build_markdown
from betbot.share import ShareError, markdown_to_html, publish_report, send_report
from betbot.sources import bookmakers, flashscore
from betbot.sources.forebet import parse_market_page, parse_predictions
from betbot.sources.forebet_pages import (
    FOREBET_PAGES,
    ForebetSaveError,
    _is_challenge,
    _wait_for_human,
)
from betbot.sources.http import FetchError

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


def _market_page(title: str, pick: str, probability: str, columns: str = "") -> str:
    """Page Forebet dediee a un marche, reduite a une rencontre."""
    return f"""
<html><head><title>{title}</title></head><body>
<div class="rcnt">
  <span class="shortTag">FR1</span>
  <span class="date_bah">26/07/2026 21:00</span>
  <span class="homeTeam"><span>Lyon</span></span>
  <span class="awayTeam"><span>Rennes</span></span>
  <div class="fprc">{columns}</div>
  <div class="predict"><span class="forepr">{pick}</span></div>
  <span class="fpr">{probability}</span>
  <div class="ex_sc">2-1</div>
  <div class="avg_sc">2.8</div>
</div>
</body></html>
"""


class TestForebetMarketPages(unittest.TestCase):
    def test_both_to_score_page_gives_both_sides(self) -> None:
        page, (prediction,) = parse_market_page(
            _market_page("Predictions Both to score | Today Forebet Football", "No", "78")
        )
        self.assertEqual(page, "both to score")
        self.assertEqual(prediction.markets["Les deux marquent : non"], 78.0)
        self.assertEqual(prediction.markets["Les deux marquent : oui"], 22.0)

    def test_under_over_page_uses_the_predicted_side(self) -> None:
        _, (prediction,) = parse_market_page(
            _market_page(
                "Predictions Under/Over 2.5 goals | Today Forebet Football", "Over", "61"
            )
        )
        self.assertEqual(prediction.markets["Plus de 2.5 buts"], 61.0)
        self.assertEqual(prediction.markets["Moins de 2.5 buts"], 39.0)

    def test_double_chance_page_uses_model_market_names(self) -> None:
        _, (prediction,) = parse_market_page(
            _market_page("Predictions Double chance | Today Forebet Football", "X1", "74")
        )
        self.assertEqual(prediction.markets, {"1N": 74.0})

    def test_half_time_page_reads_the_three_columns(self) -> None:
        _, (prediction,) = parse_market_page(
            _market_page(
                "Predictions Half Time (HT) | Today Forebet Football",
                "2",
                "51",
                columns="<span>10</span><span>39</span><span>51</span>",
            )
        )
        self.assertEqual(prediction.markets["1 (1re mi-temps)"], 10.0)
        self.assertEqual(prediction.markets["N (1re mi-temps)"], 39.0)
        self.assertEqual(prediction.markets["2 (1re mi-temps)"], 51.0)

    def test_the_1x2_page_carries_the_forebet_forecast(self) -> None:
        """Partant du listing du bookmaker, c'est la seule source du pronostic Forebet."""
        _, (prediction,) = parse_market_page(
            _market_page(
                "Predictions 1X2 | Today Forebet Football",
                "1",
                "48",
                columns="<span>48</span><span>31</span><span>22</span>",
            )
        )
        self.assertEqual(prediction.markets, {"1": 48.0, "N": 31.0, "2": 22.0})
        self.assertEqual(
            (prediction.prob_home, prediction.prob_draw, prediction.prob_away), (48.0, 31.0, 22.0)
        )
        self.assertEqual(prediction.predicted_score, "2-1")

    def test_rejects_an_unrelated_page(self) -> None:
        with self.assertRaises(FetchError):
            parse_market_page("<html><head><title>Forebet</title></head></html>")


class TestDiscoverMarketPages(unittest.TestCase):
    def test_finds_saved_pages_in_the_current_folder(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Predictions Both to score _ Today Forebet Football.htm").touch()
            (root / "Predictions Double chance _ Today Forebet Football.html").touch()
            (root / "Forebet.htm").touch()
            with (
                mock.patch("betbot.cli.Path.cwd", return_value=root),
                mock.patch.object(sys, "argv", [str(root / "Bet.Bot.exe")]),
            ):
                found = {path.name for path in discover_market_pages()}
        self.assertEqual(
            found,
            {
                "Predictions Both to score _ Today Forebet Football.htm",
                "Predictions Double chance _ Today Forebet Football.html",
            },
        )


class TestOpenReport(unittest.TestCase):
    """Le rapport s'ouvre tout seul dans l'application associee aux fichiers Markdown."""

    def test_windows_uses_the_default_application(self) -> None:
        report = Path("out") / "rapport.md"
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch("betbot.cli.os.startfile", create=True) as startfile,
        ):
            open_report(report)
        startfile.assert_called_once_with(report)

    def test_notepad_takes_over_without_a_markdown_association(self) -> None:
        """Beaucoup de machines n'associent aucune application aux fichiers .md."""
        report = Path("out") / "rapport.md"
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch("betbot.cli.os.startfile", create=True, side_effect=OSError("pas d'appli")),
            mock.patch("betbot.cli.subprocess.run") as run,
        ):
            open_report(report)
        run.assert_called_once_with(["notepad.exe", str(report)], check=False)

    def test_a_missing_viewer_does_not_fail_the_run(self) -> None:
        """Le chemin vient d'etre affiche : ne pas pouvoir l'ouvrir n'est pas une erreur."""
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch("betbot.cli.os.startfile", create=True, side_effect=OSError("pas d'appli")),
            mock.patch("betbot.cli.subprocess.run", side_effect=OSError("pas de bloc-notes")),
        ):
            open_report(Path("out") / "rapport.md")


class TestForebetPages(unittest.TestCase):
    """Enregistrement automatique des pages Forebet."""

    def test_every_page_has_a_predictions_filename(self) -> None:
        # C'est ce prefixe qui fait ramasser les fichiers par l'analyse suivante.
        for filename, url in FOREBET_PAGES.items():
            self.assertTrue(filename.startswith("Predictions"), filename)
            self.assertTrue(filename.endswith(".htm"), filename)
            self.assertTrue(url.startswith("https://www.forebet.com/"), url)

    def test_the_cloudflare_wait_page_is_recognised(self) -> None:
        self.assertTrue(_is_challenge("Just a moment...", "<html>cf-chl</html>"))
        self.assertFalse(_is_challenge("Predictions 1X2 | Today Forebet Football", "<html>"))

    def test_a_masked_window_stops_instead_of_saving_the_wait_page(self) -> None:
        page = mock.Mock()
        page.title.return_value = "Just a moment..."
        page.content.return_value = "<html></html>"
        with self.assertRaises(ForebetSaveError):
            _wait_for_human(page, headless=True)


class TestShare(unittest.TestCase):
    """Diffusion du rapport : conversion HTML, courriel, WordPress."""

    MARKDOWN = (
        "# Bet.Bot - rapport\n\n> Avertissement\n\n## Ticket\n"
        "| Match | Cote |\n| --- | --- |\n| Lyon vs Rennes | 1.55 |\n\n"
        "Valeur : **+12 %**\n"
    )

    def test_markdown_becomes_html(self) -> None:
        html = markdown_to_html(self.MARKDOWN)
        self.assertIn("<h1>Bet.Bot - rapport</h1>", html)
        self.assertIn("<blockquote><p>Avertissement</p></blockquote>", html)
        self.assertIn("<th>Match</th>", html)
        self.assertIn("<td>Lyon vs Rennes</td>", html)
        self.assertIn("<strong>+12 %</strong>", html)
        self.assertNotIn("| --- |", html)

    def test_html_is_escaped(self) -> None:
        self.assertIn("&lt;script&gt;", markdown_to_html("<script>alert(1)</script>"))

    def test_mail_needs_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "rapport.md"
            report.write_text(self.MARKDOWN, encoding="utf-8")
            config = MailConfig(user="", password="")
            with self.assertRaises(ShareError):
                send_report(config, report, [report], "kaelmi@example.com")

    def test_mail_carries_the_report_and_its_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "rapport.md"
            report.write_text(self.MARKDOWN, encoding="utf-8")
            data = Path(folder) / "donnees.json"
            data.write_text("{}", encoding="utf-8")
            config = MailConfig(host="smtp.test", port=587, user="a@b.c", password="secret")
            with mock.patch("betbot.share.smtplib.SMTP") as smtp:
                send_report(config, report, [report, data], "kaelmi@example.com")
            message = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
        self.assertEqual(message["To"], "kaelmi@example.com")
        self.assertEqual(message["Subject"], "Bet.Bot - rapport")
        self.assertEqual(
            [part.get_filename() for part in message.iter_attachments()],
            ["rapport.md", "donnees.json"],
        )

    def test_wordpress_posts_a_draft(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "rapport.md"
            report.write_text(self.MARKDOWN, encoding="utf-8")
            config = WordPressConfig(
                site="https://exemple.fr/", user="mikael", password="mot de passe"
            )
            response = mock.Mock(status_code=201)
            response.json.return_value = {"link": "https://exemple.fr/?p=12"}
            with mock.patch("betbot.share.requests.post", return_value=response) as post:
                link = publish_report(config, report)
        self.assertEqual(link, "https://exemple.fr/?p=12")
        self.assertEqual(post.call_args.args[0], "https://exemple.fr/wp-json/wp/v2/posts")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["status"], "draft")
        self.assertEqual(payload["title"], "Bet.Bot - rapport")
        self.assertIn("<h1>", payload["content"])

    def test_wordpress_refusal_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "rapport.md"
            report.write_text(self.MARKDOWN, encoding="utf-8")
            config = WordPressConfig(site="https://exemple.fr", user="a", password="b")
            response = mock.Mock(status_code=401, text="Unauthorized")
            with (
                mock.patch("betbot.share.requests.post", return_value=response),
                self.assertRaises(ShareError),
            ):
                publish_report(config, report)


class TestMergeForebetMarkets(unittest.TestCase):
    def test_matches_on_approximate_team_names(self) -> None:
        prediction = ForebetPrediction(home_team="Olympique Lyonnais", away_team="Stade Rennais")
        extra = ForebetPrediction(
            home_team="Lyon", away_team="Rennes", markets={"Plus de 2.5 buts": 61.0}
        )
        merge_forebet_markets([prediction], [extra])
        self.assertEqual(prediction.markets, {"Plus de 2.5 buts": 61.0})

    def test_the_1x2_page_fills_the_forebet_forecast(self) -> None:
        """Sans elle, une rencontre venue d'Unibet n'a aucun pronostic Forebet a comparer."""
        prediction = ForebetPrediction(home_team="Olympique Lyonnais", away_team="Stade Rennais")
        extra = ForebetPrediction(
            home_team="Lyon",
            away_team="Rennes",
            markets={"1": 48.0, "N": 31.0, "2": 22.0},
            prob_home=48.0,
            prob_draw=31.0,
            prob_away=22.0,
            predicted_score="2-1",
        )
        merge_forebet_markets([prediction], [extra])
        self.assertEqual(prediction.prob_home, 48.0)
        self.assertEqual(prediction.predicted_score, "2-1")

    def test_a_known_forecast_is_not_overwritten(self) -> None:
        prediction = ForebetPrediction(home_team="Lyon", away_team="Rennes", prob_home=55.0)
        extra = ForebetPrediction(home_team="Lyon", away_team="Rennes", prob_home=48.0)
        merge_forebet_markets([prediction], [extra])
        self.assertEqual(prediction.prob_home, 55.0)

    def test_leaves_unknown_fixtures_untouched(self) -> None:
        prediction = ForebetPrediction(home_team="Lyon", away_team="Rennes")
        extra = ForebetPrediction(
            home_team="Lille", away_team="Nantes", markets={"Plus de 2.5 buts": 61.0}
        )
        merge_forebet_markets([prediction], [extra])
        self.assertEqual(prediction.markets, {})


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

    def test_returns_none_without_form_nor_odds(self) -> None:
        stats = demo.stats_for("Getafe", "Athletic Bilbao")
        stats_without_form = type(stats)(home_team=stats.home_team, away_team=stats.away_team)
        self.assertIsNone(poisson.compute(stats_without_form))

    def test_form_alone_is_flagged_as_such(self) -> None:
        result = poisson.compute(self.stats, model=poisson.MODEL_MARKET)
        self.assertEqual(result.source, poisson.SOURCE_FORM)

    def test_the_form_model_is_the_default(self) -> None:
        """Le modele d'origine reste celui applique sans option : c'est le choix du joueur."""
        self.assertEqual(poisson.compute(self.stats).source, poisson.SOURCE_FORM_ONLY)

    def test_the_form_model_ignores_the_odds(self) -> None:
        """Les cotes ne corrigent pas le modele d'origine, elles mesurent son ecart."""
        odds = {"1": 3.70, "N": 3.80, "2": 1.70}
        alone = poisson.compute(self.stats)
        priced = poisson.compute(self.stats, odds)
        self.assertEqual(alone.markets, priced.markets)
        self.assertIsNone(alone.calibration_gap)
        self.assertGreater(priced.calibration_gap, 0)


class TestMarketCalibration(unittest.TestCase):
    """Le modele doit reproduire les probabilites du marche, marge retiree."""

    ODDS: ClassVar[dict[str, float]] = {
        "1": 3.70,
        "N": 3.80,
        "2": 1.70,
        "Les deux marquent : oui": 1.45,
    }

    def test_devig_removes_the_margin(self) -> None:
        probabilities = poisson.devig(self.ODDS, ("1", "N", "2"))
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=6)
        self.assertLess(probabilities["1"], 1 / self.ODDS["1"])

    def test_devig_needs_every_outcome(self) -> None:
        self.assertIsNone(poisson.devig({"1": 2.0}, ("1", "N", "2")))

    def test_fitted_model_matches_the_market(self) -> None:
        target = poisson.devig(self.ODDS, ("1", "N", "2"))
        result = poisson.compute(
            MatchStats(home_team="A", away_team="B"), self.ODDS, model=poisson.MODEL_MARKET
        )
        self.assertEqual(result.source, poisson.SOURCE_MARKET)
        for outcome, probability in target.items():
            self.assertAlmostEqual(result.markets[outcome], 100 * probability, delta=2.0)
        self.assertLess(result.calibration_gap, 3.0)

    def test_odds_override_a_contradicting_form(self) -> None:
        """Cinq matchs de forme ne doivent pas renverser un favori du marche.

        Cas reel : Coleraine, prolifique dans son championnat, donne gagnant a 70 % par
        l'ancien modele face a HJK Helsinki, quand le marche le donnait a 24 %.
        """
        prolific = TeamForm(
            name="Coleraine",
            last_results=["W"] * 5,
            goals_for=13,
            goals_against=8,
            matches_played=5,
        )
        modest = TeamForm(
            name="HJK", last_results=["W", "W", "L"], goals_for=5, goals_against=9, matches_played=3
        )
        stats = MatchStats(
            home_team="Coleraine", away_team="HJK", home_form=prolific, away_form=modest
        )
        result = poisson.compute(stats, self.ODDS, model=poisson.MODEL_MARKET)
        self.assertLess(result.markets["1"], 40.0)
        self.assertLess(result.expected_home_goals, 2.0)
        # Le modele de forme, lui, maintient son favori : c'est sa nature, et le rapport
        # doit publier l'ecart plutot que le corriger.
        optimistic = poisson.compute(stats, self.ODDS, model=poisson.MODEL_FORM)
        self.assertGreater(optimistic.markets["1"], 60.0)
        self.assertGreater(optimistic.calibration_gap, 30.0)

    def test_dixon_coles_adds_weight_to_the_goalless_draw(self) -> None:
        """Deux Poisson independantes sous-estiment le 0-0, donc le « BTTS non »."""
        matrix = poisson.score_matrix(1.3, 1.1)
        self.assertGreater(matrix[0][0], exp(-1.3) * exp(-1.1))
        self.assertAlmostEqual(sum(sum(row) for row in matrix), 1.0, places=6)

    def test_the_form_model_keeps_independent_poisson(self) -> None:
        """Le modele d'origine n'applique pas Dixon-Coles : il reste reproductible tel quel."""
        matrix = poisson.score_matrix(1.3, 1.1, dixon_coles=False)
        self.assertAlmostEqual(matrix[0][0], exp(-1.3) * exp(-1.1), delta=1e-15)

    def test_the_form_model_matrix_is_not_renormalised(self) -> None:
        """La renormalisation deplacerait les probabilites publiees par la V1.

        Le modele d'origine tronque les scores a huit buts sans redistribuer la masse
        perdue : sur une equipe tres prolifique, renormaliser suffirait a changer le
        « les deux marquent » de plusieurs dixiemes de point.
        """
        matrix = poisson.score_matrix(3.5, 2.8, dixon_coles=False)
        self.assertLess(sum(sum(row) for row in matrix), 1.0)
        self.assertAlmostEqual(matrix[2][1], exp(-3.5) * 3.5**2 / 2 * exp(-2.8) * 2.8, places=12)

    def test_the_form_model_btts_matches_the_v1_formula(self) -> None:
        """« Les deux marquent : oui » doit rester le produit de deux Poisson tronquees."""
        prolific = TeamForm(
            name="A", last_results=["W"] * 5, goals_for=12, goals_against=9, matches_played=5
        )
        opponent = TeamForm(
            name="B", last_results=["D"] * 5, goals_for=10, goals_against=11, matches_played=5
        )
        stats = MatchStats(home_team="A", away_team="B", home_form=prolific, away_form=opponent)
        result = poisson.compute(stats)

        def scores_at_least_once(lam: float) -> float:
            return sum(exp(-lam) * lam**goals / factorial(goals) for goals in range(1, 9))

        expected = 100 * (
            scores_at_least_once(result.expected_home_goals)
            * scores_at_least_once(result.expected_away_goals)
        )
        # Les buts attendus sont publies arrondis au centieme : la comparaison ne peut
        # pas etre plus fine que l'arrondi lui-meme.
        self.assertAlmostEqual(result.markets["Les deux marquent : oui"], expected, delta=0.1)


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
        "e1": {
            "a": "Lyon",
            "b": "Rennes",
            "pdesc": "Ligue 1",
            "start": "2607252100",
            "desc": "Lyon vs Stade Rennais",
            "path": {"Category": "France", "League": "Ligue 1"},
        },
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

    def test_builds_predictions_from_the_odds_grid(self) -> None:
        entries = bookmakers._parse_unibet_page(UNIBET_PAGE)
        (prediction,) = predictions_from_odds(entries + entries, limit=10)
        self.assertEqual((prediction.home_team, prediction.away_team), ("Lyon", "Rennes"))
        self.assertEqual(prediction.competition, "Ligue 1")
        self.assertIsNone(prediction.best_probability)

    def test_reads_the_kickoff_day(self) -> None:
        self.assertEqual(bookmakers.kickoff_day("2026-07-25 21:00"), "2026-07-25")
        self.assertIsNone(bookmakers.kickoff_day(None))

    def test_reads_the_kickoff_time(self) -> None:
        (entry,) = bookmakers._parse_unibet_page(UNIBET_PAGE)
        self.assertEqual(entry.kickoff, "2026-07-25 21:00")

    def test_builds_the_detail_page_url(self) -> None:
        (entry,) = bookmakers._parse_unibet_page(UNIBET_PAGE)
        self.assertEqual(
            entry.url,
            "https://www.unibet.fr/paris-football/france/ligue-1/1/lyon-vs-stade-rennais",
        )


def _card(title: str, rows: list[tuple[str, str]], *, labelled: bool = False) -> str:
    """Reproduit une carte de marche de la page detail Unibet."""
    cells = []
    for label, price in rows:
        button = f'<button><span class="psel-outcome__data">{price}</span></button>'
        if labelled:
            button = (
                f'<button><span class="psel-outcome__label">{label}</span>'
                f'<span class="psel-outcome__data">{price}</span></button>'
            )
            cells.append(f"<td><psel-outcome>{button}</psel-outcome></td>")
        else:
            cells.append(
                f'<tr><th class="psel-market__head">{label}</th>'
                f"<td><psel-outcome>{button}</psel-outcome></td></tr>"
            )
    body = f"<tr>{''.join(cells)}</tr>" if labelled else "".join(cells)
    return (
        '<div class="psel-market-card">'
        f'<span class="psel-title-market__label">{title}</span>'
        f"<table><tbody>{body}</tbody></table></div>"
    )


UNIBET_DETAIL = "".join(
    [
        _card(
            "Double Chance - 90 Mins",
            [("Lyon / N", "1,30"), ("N / Rennes", "1,55"), ("Lyon / Rennes", "1,25")],
            labelled=True,
        ),
        _card("Les 2 \u00e9quipes marqueront-elles? - 90 Mins", [("Oui", "1,80"), ("Non", "1,95")]),
        _card(
            "R\u00e9sultat et les deux \u00e9quipes marquent - 90 Mins",
            [("Lyon / Oui", "3,05"), ("N / Oui", "5,40"), ("Rennes / Non", "8,20")],
        ),
        _card(
            "Double chance et les 2 \u00e9quipes marquent - 90 Mins",
            [("Lyon / N et Oui", "2,10"), ("N / Rennes et Non", "4,80")],
        ),
        _card("R\u00e9sultat et Plus/Moins Buts - 90 Mins", [("Lyon et plus 1,5", "1,90")]),
        _card(
            "Plus / Moins Buts - 90 Mins",
            [("Plus 2.5", "1,55"), ("Moins 2.5", "1,90")],
            labelled=True,
        ),
        _card(
            "Double chance et Plus/Moins Buts - 90 Mins",
            [("Lyon / Nul et plus de 2.5 buts", "1,80")],
        ),
        # Les deux equipes marquent par periode : premiere colonne = premiere mi-temps.
        _card(
            "Les 2 \u00e9quipes marqueront elles ? - P\u00e9riodes",
            [("Oui", "3,25"), ("Non", "1,15")],
        ),
        # Marche sans equivalent dans le modele : doit etre ignore.
        _card("Buteurs - 90 Mins", [("Alexandre Lacazette", "2,50")]),
    ]
)


class TestDetailedMarkets(unittest.TestCase):
    def setUp(self) -> None:
        self.odds = bookmakers.parse_event_markets(UNIBET_DETAIL, "Lyon", "Rennes")

    def test_reads_both_teams_to_score(self) -> None:
        self.assertEqual(self.odds["Les deux marquent : oui"], 1.80)
        self.assertEqual(self.odds["Les deux marquent : non"], 1.95)

    def test_reads_double_chance(self) -> None:
        self.assertEqual(self.odds["1N"], 1.30)
        self.assertEqual(self.odds["N2"], 1.55)
        self.assertEqual(self.odds["12"], 1.25)

    def test_reads_combined_markets(self) -> None:
        self.assertEqual(self.odds["1 et oui"], 3.05)
        self.assertEqual(self.odds["N et oui"], 5.40)
        self.assertEqual(self.odds["2 et non"], 8.20)
        self.assertEqual(self.odds["1N et oui"], 2.10)
        self.assertEqual(self.odds["N2 et non"], 4.80)

    def test_reads_goal_lines(self) -> None:
        self.assertEqual(self.odds["Plus de 2.5 buts"], 1.55)
        self.assertEqual(self.odds["Moins de 2.5 buts"], 1.90)
        self.assertEqual(self.odds["1 et plus de 1.5 buts"], 1.90)
        self.assertEqual(self.odds["1N et plus de 2.5 buts"], 1.80)

    def test_reads_first_half_both_teams_to_score(self) -> None:
        self.assertEqual(self.odds[poisson.FIRST_HALF_BTTS], 3.25)

    def test_ignores_markets_absent_from_the_model(self) -> None:
        self.assertNotIn("Alexandre Lacazette", self.odds)
        self.assertEqual(len(self.odds), 15)

    def test_every_market_read_exists_in_the_model(self) -> None:
        stats = demo.stats_for("Olympique Lyonnais", "Stade Rennais")
        known = poisson.compute(stats).markets
        for market in self.odds:
            self.assertIn(market, known)


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

    def test_value_follows_the_price_and_the_model(self) -> None:
        bundle = self._bundle()
        opportunities = bundle.opportunities()
        for _market, odds, probability, value in opportunities:
            self.assertAlmostEqual(value, 100 * (odds * probability / 100 - 1), places=1)
        self.assertEqual(opportunities, sorted(opportunities, key=lambda i: -i[3]))

    def test_range_excludes_prices_outside_the_window(self) -> None:
        markets = {item[0] for item in self._bundle().opportunities((1.65, 1.95))}
        self.assertEqual(markets, {"1"})


class TestKelly(unittest.TestCase):
    def test_no_stake_without_an_edge(self) -> None:
        self.assertEqual(kelly_share(50.0, 1.90), 0.0)
        self.assertEqual(kelly_share(90.0, 1.0), 0.0)

    def test_stake_grows_with_the_edge(self) -> None:
        small = kelly_share(55.0, 1.90)
        large = kelly_share(60.0, 1.90)
        self.assertGreater(small, 0)
        self.assertGreater(large, small)
        # Un quart de Kelly : (p * cote - 1) / (cote - 1) / 4 pour 60 % a 1.90.
        self.assertAlmostEqual(large, 0.25 * (0.60 * 1.90 - 1) / 0.90, places=6)

    def test_stake_is_capped(self) -> None:
        """Une probabilite surestimee ne doit pas faire conseiller un tiers du capital."""
        self.assertEqual(kelly_share(95.0, 1.35), KELLY_MAX_SHARE)


class TestCombo(unittest.TestCase):
    def setUp(self) -> None:
        self.bundles = build_bundles(demo.predictions(), AppConfig(), offline=True)

    def test_ticket_probability_is_the_product_of_its_legs(self) -> None:
        ticket = build_ticket(self.bundles, legs=2)
        assert ticket is not None
        expected = ticket.legs[0].probability * ticket.legs[1].probability / 100
        self.assertAlmostEqual(ticket.probability, expected, places=1)

    def test_adding_a_leg_lowers_the_probability(self) -> None:
        two = build_ticket(self.bundles, legs=1)
        three = build_ticket(self.bundles, legs=2)
        assert two is not None and three is not None
        self.assertLess(three.probability, two.probability)

    def test_fair_odds_is_the_inverse_of_the_probability(self) -> None:
        ticket = build_ticket(self.bundles, legs=2)
        assert ticket is not None
        self.assertAlmostEqual(ticket.fair_odds, 100 / ticket.probability, places=1)

    def test_market_option_forces_every_leg(self) -> None:
        ticket = build_ticket(self.bundles, legs=2, market="Les deux marquent : oui")
        assert ticket is not None
        self.assertTrue(all(leg.market == "Les deux marquent : oui" for leg in ticket.legs))

    def test_legs_are_ordered_by_kickoff(self) -> None:
        ticket = build_ticket(self.bundles, legs=2)
        assert ticket is not None
        kickoffs = [leg.kickoff for leg in ticket.legs]
        self.assertEqual(kickoffs, sorted(kickoffs))

    def test_deadline_is_the_first_kickoff(self) -> None:
        ticket = build_ticket(self.bundles, legs=2)
        assert ticket is not None
        self.assertEqual(ticket.deadline, "2026-07-26 19:00")

    def test_unreachable_threshold_returns_no_ticket(self) -> None:
        self.assertIsNone(build_ticket(self.bundles, legs=2, min_probability=99.9))


class TestValueTicket(unittest.TestCase):
    """Combines longs a marches melanges, batis sur les cotes reellement disponibles."""

    def _bundles(self, count: int) -> list[MatchBundle]:
        template = demo.stats_for("Olympique Lyonnais", "Stade Rennais")
        assert template is not None
        bundles = []
        for index in range(count):
            stats = replace(
                template,
                home_team=f"Equipe {index}",
                kickoff=f"2026-07-26 1{index}:00",
            )
            bundles.append(
                MatchBundle(
                    stats=stats,
                    poisson=poisson.compute(stats),
                    bookmakers=[
                        BookmakerLine(
                            bookmaker="Unibet",
                            odds={"1": 1.80, "X": 3.60, "2": 4.20, "1N": 1.25, "12": 1.20},
                        )
                    ],
                )
            )
        return bundles

    def test_takes_one_priced_selection_per_match(self) -> None:
        ticket = build_value_ticket(self._bundles(6), legs=6)
        assert ticket is not None
        self.assertEqual(len(ticket.legs), 6)
        self.assertEqual(len({leg.match for leg in ticket.legs}), 6)
        self.assertTrue(all(leg.odds for leg in ticket.legs))

    def test_rejects_legs_the_model_judges_unlikely(self) -> None:
        ticket = build_value_ticket(self._bundles(6), legs=6, min_leg_probability=60.0)
        assert ticket is not None
        self.assertTrue(all(leg.probability >= 60.0 for leg in ticket.legs))

    def test_returns_nothing_without_enough_priced_matches(self) -> None:
        self.assertIsNone(build_value_ticket(self._bundles(3), legs=8))

    def test_the_form_model_picks_the_likeliest_leg_not_the_widest_gap(self) -> None:
        """Sans calibration, la plus grosse valeur affichee est la plus grosse erreur.

        Le modele de forme est plus tranche que le marche : la cote 4.20 sur l'exterieur
        y semble une aubaine alors qu'elle reste le resultat le moins probable.
        """
        ticket = build_value_ticket(self._bundles(6), legs=6)
        assert ticket is not None
        self.assertTrue(all(leg.market != "2" for leg in ticket.legs))
        self.assertTrue(all(leg.probability >= 55.0 for leg in ticket.legs))


class TestFlashscoreSearch(unittest.TestCase):
    """Choix du bon club parmi les resultats de recherche, sans appeler le reseau."""

    def _best(self, name: str, titles: list[str], competition: str | None = None) -> str:
        country = flashscore.country_hint(competition)
        return max(titles, key=lambda title: flashscore.score_candidate(name, title, country))

    def test_prefers_the_club_over_a_namesake_abroad(self) -> None:
        titles = ["Libertad Asuncion (Paraguay)", "Libertad (Ecuador)", "Libertad FC (Bolivia)"]
        self.assertEqual(self._best("Libertad Loja", titles, "D1 Equateur"), "Libertad (Ecuador)")

    def test_ignores_womens_and_youth_squads(self) -> None:
        titles = ["Hacken W (Sweden)", "Hacken U19 (Sweden)", "Hacken (Sweden)"]
        self.assertEqual(self._best("H\u00e4cken", titles), "Hacken (Sweden)")

    def test_reads_glued_and_abbreviated_names(self) -> None:
        titles = ["FC Tiraspol (Moldova)", "Sheriff Tiraspol (Moldova)"]
        self.assertEqual(self._best("SherifTiraspol", titles), "Sheriff Tiraspol (Moldova)")
        self.assertEqual(
            self._best("Universit Cluj", ["CFR Cluj (Romania)", "U. Cluj (Romania)"]),
            "U. Cluj (Romania)",
        )

    def test_query_variants_spread_glued_names(self) -> None:
        self.assertIn("Mac Tel Aviv", flashscore.query_variants("Mac.Tel Aviv"))
        self.assertIn("Sherif Tiraspol", flashscore.query_variants("SherifTiraspol"))

    def test_country_hint_translates_the_competition(self) -> None:
        self.assertEqual(flashscore.country_hint("D1 Br\u00e9sil"), "d1 brazil")
        self.assertEqual(flashscore.country_hint("D1 Paraguay"), "d1 paraguay")
        self.assertIsNone(flashscore.country_hint(None))

    def test_matches_dotted_abbreviations_and_other_spellings(self) -> None:
        self.assertGreaterEqual(
            flashscore.score_candidate("Dynamo Kiev", "Dyn. Kyiv (Ukraine)"),
            flashscore.NAME_THRESHOLD,
        )
        self.assertGreaterEqual(
            flashscore.score_candidate("FK DAC 1904", "DAC Dunajska Streda (Slovakia)"),
            flashscore.NAME_THRESHOLD,
        )
        self.assertGreater(
            flashscore.score_candidate("Dynamo Minsk", "Dinamo Minsk (Belarus)"),
            flashscore.score_candidate("Dynamo Minsk", "FC Minsk (Belarus)"),
        )

    def test_reads_the_abbreviations_of_the_bookmaker(self) -> None:
        """« Utd », « SL », « NY » : le bookmaker abrege, Flashscore ecrit en entier."""
        self.assertEqual(
            self._best("Cambrian Utd", ["Manchester Utd (England)", "Cambrian United (Wales)"]),
            "Cambrian United (Wales)",
        )
        self.assertEqual(
            self._best("Henan SL", ["Henan Songshan Longmen (China)", "Henanger (Norway)"]),
            "Henan Songshan Longmen (China)",
        )
        self.assertEqual(
            self._best("NY City FC", ["Manchester City (England)", "New York City (USA)"]),
            "New York City (USA)",
        )
        # Une abreviation qui designerait deux mots du meme nom reste telle quelle.
        self.assertLess(
            flashscore.score_candidate("St Gilloise", "Saint Etienne (France)"),
            flashscore.NAME_THRESHOLD,
        )

    def test_query_variants_spell_out_city_initials(self) -> None:
        """La recherche ne renvoie rien sur « NY » : le sigle doit etre deplie."""
        self.assertIn("new york City FC", flashscore.query_variants("NY City FC"))

    def test_translates_the_french_club_names(self) -> None:
        self.assertEqual(flashscore.alias("La Gantoise"), "Gent")
        self.assertIn("Gent", flashscore.query_variants("La Gantoise"))
        self.assertIn("dinamo kyiv", flashscore.query_variants("Dynamo Kiev"))
        self.assertIsNone(flashscore.alias("Lyon"))

    def test_an_unrelated_name_stays_below_the_threshold(self) -> None:
        score = flashscore.score_candidate("Dunav Rousse", "Monticello (France)")
        self.assertLess(score, flashscore.NAME_THRESHOLD)

    def test_falls_back_to_the_namesake_whose_page_has_matches(self) -> None:
        empty = flashscore.Team("1", "vitoria-amateur", "Vitoria (Brazil)")
        played = flashscore.Team("2", "vitoria", "Vitoria (Brazil)")
        results = {played: [flashscore.PastMatch("01.01.", "Vitoria", "Bahia", 1, 0)]}
        with mock.patch.object(
            flashscore, "fetch_team_results", lambda team, cfg: results.get(team, [])
        ):
            team, matches = flashscore.team_with_results([empty, played], ScrapeConfig())
        self.assertEqual(team, played)
        self.assertEqual(len(matches), 1)

    def test_reports_when_no_namesake_has_matches(self) -> None:
        team = flashscore.Team("1", "vitoria-amateur", "Vitoria (Brazil)")
        with (
            mock.patch.object(flashscore, "fetch_team_results", lambda team, cfg: []),
            self.assertRaises(flashscore.FlashscoreUnavailable),
        ):
            flashscore.team_with_results([team], ScrapeConfig())


class TestPipelineOffline(unittest.TestCase):
    def test_builds_bundles_and_report_without_network(self) -> None:
        bundles = build_bundles(demo.predictions(), AppConfig(), offline=True)
        self.assertEqual(len(bundles), 2)
        self.assertTrue(all(bundle.poisson for bundle in bundles))

        markdown = build_markdown([(bundle, None) for bundle in bundles])
        self.assertIn("Olympique Lyonnais vs Stade Rennais", markdown)
        self.assertIn("| Poisson (", markdown)


if __name__ == "__main__":
    unittest.main()
