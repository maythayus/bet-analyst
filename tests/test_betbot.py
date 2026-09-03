"""Tests hors ligne : `python -m unittest discover -s tests`."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from math import exp, factorial
from pathlib import Path
from typing import ClassVar
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from betbot import (
    consensus,
    demo,
    llm,
    pipeline,
    poisson,
    priority,
    report,
    strength,
    tracking,
    trap,
)
from betbot.cli import discover_market_pages, open_report
from betbot.combo import (
    BTTS_NO,
    BTTS_YES,
    KELLY_MAX_SHARE,
    SOURCE_CONSENSUS,
    SOURCE_MODEL,
    _leg_for,
    build_max_ticket,
    build_ticket,
    build_value_ticket,
    kelly_share,
)
from betbot.config import (
    AppConfig,
    LMStudioConfig,
    MailConfig,
    ScrapeConfig,
    WordPressConfig,
)
from betbot.llm import LMStudioClient, LMStudioError
from betbot.models import (
    PROMPT_HEAD_TO_HEAD,
    PROMPT_MATCHES,
    BookmakerLine,
    ForebetPrediction,
    MatchBundle,
    MatchStats,
    PlayedMatch,
    PoissonResult,
    TableStanding,
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
from betbot.sources import bookmakers, flashscore, forebet
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

    def test_the_french_pages_are_read_the_same_way(self) -> None:
        """Ce sont celles que Bet.Bot enregistre : leurs titres et pronostics sont en
        francais, accents compris."""
        page, (btts,) = parse_market_page(
            _market_page(
                "Pronostics Chaque \u00e9quipe marque | Forebet Football", "Oui", "63"
            )
        )
        self.assertEqual(page, "both to score")
        self.assertEqual(btts.markets["Les deux marquent : oui"], 63.0)
        self.assertEqual(btts.markets["Les deux marquent : non"], 37.0)

        page, (goals,) = parse_market_page(
            _market_page("Pronostics Moins-Plus 2.5 de buts | Forebet", "Plus", "58")
        )
        self.assertEqual(page, "under/over 2.5 goals")
        self.assertEqual(goals.markets["Plus de 2.5 buts"], 58.0)

        page, (chance,) = parse_market_page(
            _market_page("Pronostics Chance double | Forebet", "1N", "77")
        )
        self.assertEqual(page, "double chance")
        self.assertEqual(chance.markets, {"1N": 77.0})

        page, (half,) = parse_market_page(
            _market_page(
                "Pronostics Mi-temps | Forebet",
                "N",
                "42",
                columns="<span>33</span><span>42</span><span>25</span>",
            )
        )
        self.assertEqual(page, "half time")
        self.assertEqual(half.markets["N (1re mi-temps)"], 42.0)

    def test_the_listing_is_recognised_by_its_real_title(self) -> None:
        """Le listing 1X2 ne dit pas « 1X2 » : Forebet l'intitule « Pronostics de
        football pour aujourd'hui », avec une apostrophe courbe."""
        page, (prediction,) = parse_market_page(
            _market_page(
                "Pronostics de football pour aujourd\u2019hui | Forebet Pronostics",
                "1",
                "52",
                columns="<span>52</span><span>27</span><span>21</span>",
            )
        )
        self.assertEqual(page, "1x2")
        self.assertEqual(prediction.markets, {"1": 52.0, "N": 27.0, "2": 21.0})

    def test_the_titles_saved_by_the_browser_are_all_recognised(self) -> None:
        """Titres reellement enregistres depuis Forebet : le marche precede « pour
        aujourd'hui », qui sert aussi au listing 1X2 — la mi-temps ne doit donc pas
        etre prise pour le listing."""
        expected = {
            "Pronostics de football pour aujourd\u2019hui | Forebet Pronostics": "1x2",
            "Moins_Plus 2.5 de buts | Forebet Pr\u00e9dictions pour aujourd\u2019hui": (
                "under/over 2.5 goals"
            ),
            "Mi-temps | Forebet Pronostics pour aujourd\u2019hui": "half time",
            "Chaque \u00e9quipe marque | Forebet Pr\u00e9dictions pour aujourd\u2019hui": (
                "both to score"
            ),
            "Chance double | Forebet Pr\u00e9dictions pour aujourd\u2019hui": "double chance",
        }
        for title, kind in expected.items():
            with self.subTest(title=title):
                page = forebet.market_page_kind(
                    f"<html><head><title>{title}</title></head><body></body></html>"
                )
                self.assertEqual(page, kind)

    def test_rejects_an_unrelated_page(self) -> None:
        with self.assertRaises(FetchError) as raised:
            parse_market_page("<html><head><title>Forebet</title></head></html>")
        # Le titre lu est cite : sans lui, impossible de savoir quoi ajouter.
        self.assertIn("Forebet", str(raised.exception))


class TestReadMarketPages(unittest.TestCase):
    """Les fichiers etant ramasses automatiquement, un intrus ne doit rien casser."""

    def test_an_unknown_page_is_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            good = root / "Chaque equipe marque _ Forebet.htm"
            good.write_text(
                _market_page("Chaque \u00e9quipe marque | Forebet", "Oui", "61"), encoding="utf-8"
            )
            intruder = root / "Forebet notes.htm"
            intruder.write_text("<html><head><title>Forebet</title></head></html>", "utf-8")
            (prediction,) = forebet.read_market_pages([intruder, good])
        self.assertEqual(prediction.markets["Les deux marquent : oui"], 61.0)

    def test_nothing_readable_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            intruder = Path(folder) / "Forebet notes.htm"
            intruder.write_text("<html><head><title>Forebet</title></head></html>", "utf-8")
            with self.assertRaises(FetchError) as raised:
                forebet.read_market_pages([intruder])
        self.assertIn("Forebet notes.htm", str(raised.exception))

    def test_a_missing_file_is_still_an_error(self) -> None:
        with self.assertRaises(FetchError):
            forebet.read_market_pages([Path("chemin/inexistant.htm")])


class TestDiscoverMarketPages(unittest.TestCase):
    def test_finds_saved_pages_in_the_current_folder(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Pronostics Chaque equipe marque _ Forebet Football.htm").touch()
            (root / "Predictions Double chance _ Today Forebet Football.html").touch()
            # Nom donne par le navigateur : il reprend le titre, donc le marche d'abord.
            (root / "Mi-temps _ Forebet Pronostics pour aujourd'hui.htm").touch()
            (root / "rapport.html").touch()
            with (
                mock.patch("betbot.cli.Path.cwd", return_value=root),
                mock.patch.object(sys, "argv", [str(root / "Bet.Bot.exe")]),
            ):
                found = {path.name for path in discover_market_pages()}
        self.assertEqual(
            found,
            {
                "Pronostics Chaque equipe marque _ Forebet Football.htm",
                "Predictions Double chance _ Today Forebet Football.html",
                "Mi-temps _ Forebet Pronostics pour aujourd'hui.htm",
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

    def test_every_page_has_a_pronostics_filename_and_a_french_url(self) -> None:
        # C'est ce prefixe qui fait ramasser les fichiers par l'analyse suivante.
        for filename, url in FOREBET_PAGES.items():
            self.assertTrue(filename.startswith("Pronostics"), filename)
            self.assertTrue(filename.endswith(".htm"), filename)
            self.assertTrue(
                url.startswith("https://www.forebet.com/fr/pronostics-pour-aujourd-hui"), url
            )

    def test_the_cloudflare_wait_page_is_recognised(self) -> None:
        self.assertTrue(_is_challenge("Just a moment...", "<html>cf-chl</html>"))
        self.assertFalse(_is_challenge("Predictions 1X2 | Today Forebet Football", "<html>"))

    def test_a_masked_window_stops_instead_of_saving_the_wait_page(self) -> None:
        page = mock.Mock()
        page.title.return_value = "Just a moment..."
        page.content.return_value = "<html></html>"
        with self.assertRaises(ForebetSaveError):
            _wait_for_human(page, headless=True)


class TestLLMPrompt(unittest.TestCase):
    """Ce qui part au modele local doit tenir dans sa fenetre de contexte."""

    def _bundle(self) -> MatchBundle:
        form = TeamForm(
            name="Lyon",
            last_results=["W", "D", "L", "W", "W"],
            goals_for=9,
            goals_against=5,
            matches_played=20,
            matches=[
                PlayedMatch(
                    opponent=f"Adversaire {index}",
                    scored=index % 3,
                    conceded=(index + 1) % 3,
                    at_home=bool(index % 2),
                    date=f"2026-01-{index + 1:02d}",
                )
                for index in range(20)
            ],
        )
        stats = MatchStats(
            home_team="Lyon",
            away_team="Rennes",
            home_form=form,
            away_form=replace(form, name="Rennes"),
            head_to_head=[f"2025-0{index + 1}-01 Lyon 2-1 Rennes" for index in range(9)],
            standings=[
                TableStanding(
                    name=f"Club {index}",
                    position=index + 1,
                    played=30,
                    wins=15 - index // 2,
                    goals_for=48 - index,
                    goals_against=33 + index,
                    points=53 - 2 * index,
                )
                for index in range(20)
            ],
        )
        return MatchBundle(stats=stats)

    def test_the_prompt_drops_what_only_the_model_needs(self) -> None:
        bundle = self._bundle()
        prompt = bundle.to_prompt_dict()
        home = prompt["flashscore"]["home_form"]
        assert home is not None
        self.assertEqual(len(home["recent_matches"]), PROMPT_MATCHES)
        self.assertEqual(len(prompt["flashscore"]["head_to_head"]), PROMPT_HEAD_TO_HEAD)
        # Le classement complet sert au modele, pas au commentaire.
        self.assertNotIn("standings", prompt["flashscore"])
        # Les moyennes restent, elles portent sur les vingt matchs.
        self.assertEqual(home["matches_played"], 20)

    def test_the_prompt_is_far_shorter_than_the_full_bundle(self) -> None:
        bundle = self._bundle()
        full = json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2, default=str)
        short = json.dumps(bundle.to_prompt_dict(), ensure_ascii=False, default=str)
        self.assertLess(len(short), len(full) / 2)

    FIRST_PASS = (
        "<think>je reflechis</think>\n### Verdict\nLyon 1N.\n### Risques\nPeu.\n\n"
        '```json\n{"decision": "Jouer", "marche": "1N", "source_moins_credible": "modele",\n'
        ' "risque_principal": "defense de Lyon", "confiance": 7}\n```'
    )
    SECOND_PASS = (
        "### Ce qui contredit l'analyse\nRennes marque a chaque sortie.\n\n"
        '```json\n{"objection": "Rennes marque partout", "verdict_maintenu": false,\n'
        ' "confiance_revisee": 4}\n```'
    )

    def test_closed_questions_are_read_and_removed_from_the_markdown(self) -> None:
        verdict = llm.parse_verdict(self.FIRST_PASS)
        assert verdict is not None
        self.assertEqual(verdict.decision, "jouer")
        self.assertEqual(verdict.market, "1N")
        self.assertEqual(verdict.least_credible, "modele")
        self.assertEqual(verdict.confidence, 7)
        stripped = llm.strip_verdict(self.FIRST_PASS)
        self.assertNotIn("```", stripped)
        self.assertIn("### Risques", stripped)

    def test_an_answer_outside_the_choices_is_not_a_decision(self) -> None:
        verdict = llm.parse_verdict('```json\n{"decision": "peut-etre", "confiance": 42}\n```')
        assert verdict is not None
        self.assertIsNone(verdict.decision)
        self.assertEqual(verdict.confidence, 10)
        self.assertIsNone(llm.parse_verdict("### Verdict\nsans bloc JSON"))

    def test_the_devils_advocate_can_overturn_and_lower_confidence(self) -> None:
        """Deux appels : le second recoit la premiere analyse, et son avis est garde
        a cote du verdict, sans toucher aux probabilites."""
        client = LMStudioClient(LMStudioConfig(second_pass=True))
        bundle = self._bundle()
        with mock.patch.object(
            client, "chat", side_effect=[self.FIRST_PASS, self.SECOND_PASS]
        ) as chat:
            analysis = client.analyse(bundle)
        self.assertEqual(chat.call_count, 2)
        self.assertIn("Lyon 1N.", chat.call_args_list[1].args[1])
        assert analysis.verdict is not None
        self.assertIs(analysis.upheld, False)
        self.assertEqual(analysis.verdict.confidence, 4)
        self.assertIn("Rennes marque", analysis.rebuttal or "")
        self.assertNotIn("<think>", analysis.markdown)
        markdown = report.build_markdown([(bundle, analysis)])
        self.assertIn("| Decision | jouer |", markdown)
        self.assertIn("| Verdict apres contre-analyse | renverse |", markdown)
        self.assertIn("#### Contre-analyse", markdown)

    def test_the_second_pass_can_be_switched_off(self) -> None:
        client = LMStudioClient(LMStudioConfig(second_pass=False))
        with mock.patch.object(client, "chat", return_value=self.FIRST_PASS) as chat:
            analysis = client.analyse(self._bundle())
        self.assertEqual(chat.call_count, 1)
        self.assertIsNone(analysis.rebuttal)
        self.assertIsNone(analysis.upheld)

    def test_a_context_overflow_says_what_to_change(self) -> None:
        """Sans ce message, le rapport sort sans commentaire et rien ne l'explique."""
        client = LMStudioClient(LMStudioConfig())
        response = mock.Mock()
        response.text = (
            "request (8300 tokens) exceeds the available context size (8192 tokens)"
        )
        error = requests.HTTPError("400 Client Error", response=response)
        with (
            mock.patch.object(client.session, "post", side_effect=error),
            self.assertRaises(LMStudioError) as raised,
        ):
            client.chat("systeme", "utilisateur")
        self.assertIn("Context Length", str(raised.exception))


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

    def test_the_form_model_applies_dixon_coles(self) -> None:
        """Le modele de forme corrige lui aussi les scores serres, comme celui du marche.

        Deux Poisson independantes sous-estiment le 0-0 et le 1-1, les deux scores les
        plus frequents du football. La correction leur redonne du poids, ce qui deplace
        les marches joues ici : « les deux marquent » et les doubles chances.
        """
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

        def btts_of(matrix: list[list[float]]) -> float:
            total = sum(sum(row) for row in matrix)
            scored = sum(matrix[home][away] for home in range(1, 9) for away in range(1, 9))
            return 100 * scored / total

        goals = (result.expected_home_goals, result.expected_away_goals)
        corrected = poisson.score_matrix(*goals)
        independent = poisson.score_matrix(*goals, dixon_coles=False)
        self.assertGreater(corrected[0][0], independent[0][0])
        self.assertGreater(corrected[1][1], independent[1][1])
        # Le modele publie bien la valeur corrigee, aux arrondis pres.
        self.assertAlmostEqual(
            result.markets["Les deux marquent : oui"], btts_of(corrected), delta=0.1
        )
        # L'ancienne formule, produit de deux Poisson tronquees, en differe desormais.
        v1 = 100 * scores_at_least_once(goals[0]) * scores_at_least_once(goals[1])
        self.assertNotAlmostEqual(result.markets["Les deux marquent : oui"], v1, delta=0.1)


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

    def test_flashscore_is_only_asked_about_matches_unibet_prices(self) -> None:
        """Sans `--only-bettable`, un match du listing Forebet absent d'Unibet n'atteint
        pas Flashscore : il n'entrera dans aucun ticket."""
        unpriced = ForebetPrediction(home_team="Brest", away_team="Nice", prob_home=70.0)
        with (
            mock.patch("betbot.pipeline.collect_odds", return_value=self.entries),
            mock.patch(
                "betbot.pipeline.forebet.fetch_predictions",
                return_value=[self.prediction, unpriced],
            ),
            mock.patch("betbot.pipeline.priority.prioritise", side_effect=lambda p, _n: p),
            mock.patch("betbot.pipeline.enrich_with_detailed_markets"),
            mock.patch("betbot.pipeline.build_bundles", return_value=[]) as built,
        ):
            pipeline.run(AppConfig(), use_llm=False)
        self.assertEqual(built.call_args.args[0], [self.prediction])

    def test_a_match_asked_by_hand_is_kept_even_unpriced(self) -> None:
        with (
            mock.patch("betbot.pipeline.collect_odds", return_value=self.entries),
            mock.patch("betbot.pipeline.enrich_with_detailed_markets"),
            mock.patch("betbot.pipeline.build_bundles", return_value=[]) as built,
        ):
            pipeline.run(AppConfig(), use_llm=False, matches=["Brest vs Nice"])
        self.assertEqual(len(built.call_args.args[0]), 1)

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
        ticket = build_ticket(self.bundles, legs=2, market="Plus de 1.5 buts")
        assert ticket is not None
        self.assertTrue(all(leg.market == "Plus de 1.5 buts" for leg in ticket.legs))

    def test_a_disagreed_match_stays_out_of_the_ticket(self) -> None:
        """Forebet et le modele se contredisent sur le BTTS de Getafe : il n'est pas joue."""
        ticket = build_ticket(self.bundles, legs=2, market=BTTS_YES)
        matches = [leg.match for leg in ticket.legs] if ticket else []
        self.assertNotIn("Getafe vs Athletic Bilbao", matches)

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


class TestMaxTicket(unittest.TestCase):
    """Combine maximum : toutes les selections valides du jour, une par match."""

    def _bundle(self, index: int, btts: float) -> MatchBundle:
        """Match dont le modele est remplace par une probabilite BTTS imposee."""
        stats = MatchStats(
            home_team=f"Equipe {index}",
            away_team=f"Visiteur {index}",
            kickoff=f"2026-07-26 1{index}:00",
        )
        return MatchBundle(
            stats=stats,
            poisson=PoissonResult(
                prob_home=40.0,
                prob_draw=30.0,
                prob_away=30.0,
                prob_over_25=50.0,
                prob_btts=btts,
                expected_home_goals=1.4,
                expected_away_goals=1.1,
                most_likely_score="1-1",
                markets={BTTS_YES: btts, BTTS_NO: 100 - btts},
            ),
            bookmakers=[BookmakerLine(bookmaker="Unibet", odds={BTTS_YES: 1.60, BTTS_NO: 2.20})],
        )

    def test_every_valid_match_enters_without_size_limit(self) -> None:
        """Dix matchs qui passent : dix selections, une par match, oui comme non."""
        bundles = [self._bundle(index, 80.0 - index) for index in range(5)]
        bundles += [self._bundle(index, 20.0 + index) for index in range(5, 10)]
        ticket = build_max_ticket(bundles)
        assert ticket is not None
        self.assertEqual(len(ticket.legs), 10)
        self.assertEqual(len({leg.match for leg in ticket.legs}), 10)
        self.assertIn("10 selections", ticket.label or "")
        self.assertEqual(ticket.deadline, "2026-07-26 10:00")

    def test_matches_under_the_threshold_stay_out(self) -> None:
        """Un match a 52/48 n'a rien au-dessus de 55 % : il ne remplit pas le ticket."""
        bundles = [self._bundle(0, 80.0), self._bundle(1, 52.0), self._bundle(2, 25.0)]
        ticket = build_max_ticket(bundles)
        assert ticket is not None
        self.assertEqual(
            sorted(leg.match for leg in ticket.legs),
            ["Equipe 0 vs Visiteur 0", "Equipe 2 vs Visiteur 2"],
        )

    def test_one_selection_is_not_a_combo(self) -> None:
        self.assertIsNone(build_max_ticket([self._bundle(0, 80.0), self._bundle(1, 50.0)]))

    def test_report_adds_the_max_ticket_only_when_it_is_longer(self) -> None:
        """Avec huit matchs valides, le combine 8 est deja le maximum : pas de doublon."""
        eight = [self._bundle(index, 80.0) for index in range(8)]
        self.assertEqual([len(t.legs) for t in report.value_tickets(eight)], [6, 8])
        nine = [*eight, self._bundle(8, 75.0)]
        self.assertEqual([len(t.legs) for t in report.value_tickets(nine)], [6, 8, 9])
        five = eight[:5]
        self.assertEqual([len(t.legs) for t in report.value_tickets(five)], [5])


class TestForebetSourcedLegs(unittest.TestCase):
    """Sur les marches que Forebet publie, les deux sources sont reunies au seuil commun."""

    def _bundle(self, *, model: float, forebet: float | None) -> MatchBundle:
        stats = MatchStats(home_team="Equipe", away_team="Visiteur", kickoff="2026-07-26 18:00")
        prediction = (
            ForebetPrediction(
                home_team="Equipe",
                away_team="Visiteur",
                markets={BTTS_YES: forebet, BTTS_NO: 100 - forebet},
            )
            if forebet is not None
            else None
        )
        return MatchBundle(
            stats=stats,
            forebet=prediction,
            poisson=PoissonResult(
                prob_home=40.0,
                prob_draw=30.0,
                prob_away=30.0,
                prob_over_25=50.0,
                prob_btts=model,
                expected_home_goals=1.4,
                expected_away_goals=1.1,
                most_likely_score="1-1",
                markets={BTTS_YES: model, BTTS_NO: 100 - model},
            ),
            bookmakers=[BookmakerLine(bookmaker="Unibet", odds={BTTS_YES: 1.60, BTTS_NO: 2.20})],
        )

    def test_the_two_sources_are_averaged_when_they_agree(self) -> None:
        """Forebet pese 60 %, le modele 40 %, et le reste d'ecart tire vers le bas."""
        leg = _leg_for(self._bundle(model=72.0, forebet=66.0), BTTS_YES, 1.60, 55.0)
        assert leg is not None
        self.assertEqual(leg.source, SOURCE_CONSENSUS)
        self.assertGreater(leg.probability, 66.0)
        self.assertLess(leg.probability, 68.4)

    def test_the_odds_settle_a_disagreement_in_favour_of_forebet(self) -> None:
        """La cote de 1.60 (57.9 % marge retiree) donne raison a Forebet, pas au modele."""
        leg = _leg_for(self._bundle(model=90.0, forebet=64.0), BTTS_YES, 1.60, 55.0)
        assert leg is not None
        self.assertEqual(leg.probability, 64.0)
        self.assertTrue(leg.source.endswith(consensus.ARBITRATED))

    def test_a_disagreement_the_odds_cannot_settle_leaves_the_market_alone(self) -> None:
        """Les deux sources aussi loin du marche l'une que l'autre : on ne joue pas."""
        self.assertIsNone(_leg_for(self._bundle(model=90.0, forebet=26.0), BTTS_YES, 1.60, 55.0))

    def test_below_the_floor_the_selection_is_rejected(self) -> None:
        """Un consensus sous le seuil ne suffit pas, meme si les deux sources se rejoignent."""
        self.assertIsNone(_leg_for(self._bundle(model=52.0, forebet=53.0), BTTS_YES, 1.60, 55.0))

    def test_the_forebet_floor_applies_even_with_a_lower_request(self) -> None:
        """Un appelant plus permissif que le seuil Forebet ne l'emporte pas sur lui."""
        self.assertIsNone(_leg_for(self._bundle(model=52.0, forebet=53.0), BTTS_YES, 1.60, 45.0))

    def test_the_model_still_answers_where_forebet_says_nothing(self) -> None:
        """Sans Forebet, le modele decide seul, au seuil demande par l'appelant."""
        leg = _leg_for(self._bundle(model=57.0, forebet=None), BTTS_YES, 1.60, 55.0)
        assert leg is not None
        self.assertEqual((leg.probability, leg.source), (57.0, SOURCE_MODEL))


class TestTeamStrength(unittest.TestCase):
    """Force mesuree sur vingt matchs : anciennete, lieu, adversaires et saison."""

    LEADER: ClassVar[TableStanding] = TableStanding(
        name="Leader", position=1, played=20, wins=16, draws=2, goals_for=44, goals_against=12
    )
    LAST: ClassVar[TableStanding] = TableStanding(
        name="Dernier", position=20, played=20, wins=2, draws=3, goals_for=14, goals_against=46
    )

    def _form(self, matches: list[PlayedMatch]) -> TeamForm:
        return TeamForm(
            name="Equipe",
            last_results=[match.result for match in matches[:5]],
            goals_for=sum(match.scored for match in matches),
            goals_against=sum(match.conceded for match in matches),
            matches_played=len(matches),
            matches=matches,
        )

    def test_recent_matches_weigh_more(self) -> None:
        """Trois buts la semaine derniere ne valent pas trois buts il y a six mois."""
        improving = self._form(
            [PlayedMatch("X", 3, 0, at_home=True)] * 3 + [PlayedMatch("X", 0, 0, at_home=True)] * 12
        )
        declining = self._form(
            [PlayedMatch("X", 0, 0, at_home=True)] * 12 + [PlayedMatch("X", 3, 0, at_home=True)] * 3
        )
        rising = strength.team_rates(improving, at_home=True)
        falling = strength.team_rates(declining, at_home=True)
        assert rising is not None and falling is not None
        self.assertGreater(rising.scored, falling.scored)
        # Les deux equipes ont le meme total de buts : seule leur repartition differe.
        self.assertEqual(improving.goals_for, declining.goals_for)

    def test_home_and_away_form_are_distinguished(self) -> None:
        """Une equipe qui marque a domicile et rien dehors n'est pas la meme au deplacement."""
        matches = [PlayedMatch("X", 3, 0, at_home=True), PlayedMatch("X", 0, 2, at_home=False)] * 6
        form = self._form(matches)
        home = strength.team_rates(form, at_home=True)
        away = strength.team_rates(form, at_home=False)
        assert home is not None and away is not None
        self.assertGreater(home.scored, away.scored)
        self.assertLess(home.conceded, away.conceded)

    def test_goals_against_the_last_are_worth_less(self) -> None:
        """Le plus gros gain de justesse : deux buts contre qui, exactement ?"""
        standings = [self.LEADER, self.LAST]
        against_last = self._form([PlayedMatch("Dernier", 2, 0, at_home=True)] * 10)
        against_leader = self._form([PlayedMatch("Leader", 2, 0, at_home=True)] * 10)
        weak = strength.team_rates(against_last, at_home=True, standings=standings)
        strong = strength.team_rates(against_leader, at_home=True, standings=standings)
        assert weak is not None and strong is not None
        self.assertLess(weak.scored, strong.scored)
        # La correction est bornee : le classement reste une mesure grossiere.
        self.assertLess(strong.scored / weak.scored, 3.0)

    def test_an_unknown_opponent_is_not_corrected(self) -> None:
        """Un club d'une autre division, absent du classement, ne se corrige pas au hasard."""
        self.assertIsNone(strength.opponent_factors("Club inconnu", [self.LEADER, self.LAST]))

    def test_the_season_anchors_a_short_form(self) -> None:
        """Deux matchs ne font pas une force : le classement pese alors davantage."""
        table = TableStanding(
            name="Equipe", position=8, played=20, wins=8, draws=5, goals_for=20, goals_against=22
        )
        short = self._form([PlayedMatch("X", 4, 0, at_home=True)] * 2)
        anchored = strength.team_rates(short, at_home=True, table=table)
        raw = strength.team_rates(short, at_home=True)
        assert anchored is not None and raw is not None
        self.assertLess(anchored.scored, raw.scored)
        self.assertGreater(anchored.scored, table.scored_per_game or 0.0)

    def test_averages_alone_still_work(self) -> None:
        """Sans detail match par match, les moyennes brutes sont renvoyees telles quelles."""
        form = TeamForm(name="Equipe", goals_for=8, goals_against=4, matches_played=4)
        rates = strength.team_rates(form, at_home=True)
        assert rates is not None
        self.assertEqual((rates.scored, rates.conceded), (2.0, 1.0))


class TestConsensus(unittest.TestCase):
    """Forebet et le modele reunis en une valeur, ou ecartes quand ils se contredisent."""

    def test_two_sources_that_agree_give_the_weighted_average(self) -> None:
        agreed = consensus.blend(70.0, 70.0)
        assert agreed is not None
        self.assertEqual((agreed.probability, agreed.source), (70.0, consensus.SOURCE_CONSENSUS))
        self.assertEqual((agreed.gap, agreed.confidence), (0.0, 1.0))

    def test_the_value_never_leaves_the_interval_of_the_two_sources(self) -> None:
        """Reunir deux avis ne cree pas de certitude : la valeur reste entre les deux."""
        agreed = consensus.blend(72.0, 61.0)
        assert agreed is not None
        self.assertLessEqual(agreed.probability, 72.0)
        self.assertGreaterEqual(agreed.probability, 61.0)

    def test_the_confidence_falls_as_the_gap_widens(self) -> None:
        """Plus les deux sources s'ecartent, plus la valeur tire vers la plus basse."""
        close = consensus.blend(70.0, 66.0)
        wide = consensus.blend(70.0, 58.0)
        assert close is not None and wide is not None
        self.assertGreater(close.confidence or 0, wide.confidence or 0)
        # A ecart egal a la tolerance, il ne resterait que la valeur prudente.
        self.assertLess(wide.probability - 58.0, 0.6 * (70.0 - 58.0))

    def test_a_gap_near_fifty_percent_is_judged_more_harshly(self) -> None:
        """Le meme ecart de 16 points : decisif autour de 50 %, benin vers les extremes."""
        self.assertIsNone(consensus.blend(58.0, 42.0))
        self.assertIsNotNone(consensus.blend(92.0, 76.0))

    def test_the_odds_settle_a_disagreement(self) -> None:
        """Le bookmaker est le mieux informe : la source qui s'en approche l'emporte."""
        arbitrated = consensus.blend(80.0, 50.0, implied=78.0)
        assert arbitrated is not None
        self.assertEqual(arbitrated.probability, 80.0)
        self.assertTrue(arbitrated.arbitrated)
        self.assertFalse(arbitrated.agreed)
        self.assertEqual(arbitrated.confidence, 0.0)

    def test_the_odds_between_the_two_settle_nothing(self) -> None:
        """A egale distance des deux, la cote ne designe personne : rien n'est retenu."""
        self.assertIsNone(consensus.blend(80.0, 50.0, implied=65.0))

    def test_a_single_source_is_kept_as_is(self) -> None:
        only_forebet = consensus.blend(64.0, None)
        only_model = consensus.blend(None, 64.0)
        assert only_forebet is not None and only_model is not None
        self.assertEqual(only_forebet.source, consensus.SOURCE_FOREBET)
        self.assertEqual(only_model.source, consensus.SOURCE_MODEL)
        self.assertFalse(only_forebet.agreed)
        self.assertIsNone(consensus.blend(None, None))

    def test_the_implied_probability_drops_the_bookmaker_margin(self) -> None:
        """Deux cotes d'issues complementaires suffisent a retirer la marge."""
        bundle = MatchBundle(
            stats=MatchStats(home_team="Equipe", away_team="Visiteur"),
            bookmakers=[
                BookmakerLine(
                    bookmaker="Unibet",
                    odds={BTTS_YES: 1.60, BTTS_NO: 2.20},
                )
            ],
        )
        implied = consensus.implied_for_market(bundle, BTTS_YES)
        assert implied is not None
        # 1/1.60 = 62.5 % cote en main, ramene a 57.9 % une fois la marge repartie.
        self.assertLess(implied, 100 / 1.60)
        self.assertAlmostEqual(implied, 57.89, places=1)

    def test_an_uncovered_market_has_no_implied_probability(self) -> None:
        bundle = MatchBundle(stats=MatchStats(home_team="Equipe", away_team="Visiteur"))
        self.assertIsNone(consensus.implied_for_market(bundle, BTTS_YES))


class TestTracking(unittest.TestCase):
    """Suivi des pronostics : enregistrement, resultat reel, et ce que valent les sources."""

    def _prediction(self, **changes: object) -> tracking.Prediction:
        base = {
            "date": "2026-07-25",
            "match": "Equipe vs Visiteur",
            "home_team": "Equipe",
            "away_team": "Visiteur",
            "market": BTTS_YES,
            "forebet": 70.0,
            "model": 60.0,
            "implied": 62.0,
            "kept": 66.0,
            "source": consensus.SOURCE_CONSENSUS,
            "odds": 1.60,
        }
        return tracking.Prediction(**{**base, **changes})

    def _bundle(self) -> MatchBundle:
        return MatchBundle(
            stats=MatchStats(home_team="Equipe", away_team="Visiteur", kickoff="2026-07-25 18:00"),
            forebet=ForebetPrediction(
                home_team="Equipe",
                away_team="Visiteur",
                markets={BTTS_YES: 66.0, BTTS_NO: 34.0},
            ),
            poisson=PoissonResult(
                prob_home=40.0,
                prob_draw=30.0,
                prob_away=30.0,
                prob_over_25=50.0,
                prob_btts=62.0,
                expected_home_goals=1.4,
                expected_away_goals=1.1,
                most_likely_score="1-1",
                markets={BTTS_YES: 62.0, BTTS_NO: 38.0},
            ),
            bookmakers=[BookmakerLine(bookmaker="Unibet", odds={BTTS_YES: 1.60, BTTS_NO: 2.20})],
        )

    def test_a_market_is_judged_on_the_real_score(self) -> None:
        self.assertTrue(tracking.outcome(BTTS_YES, 2, 1))
        self.assertFalse(tracking.outcome(BTTS_YES, 2, 0))
        self.assertTrue(tracking.outcome(BTTS_NO, 2, 0))
        self.assertTrue(tracking.outcome("1N", 1, 1))
        self.assertFalse(tracking.outcome("N2", 2, 1))
        self.assertTrue(tracking.outcome("12", 2, 1))
        self.assertTrue(tracking.outcome("Plus de 2.5 buts", 2, 1))
        self.assertFalse(tracking.outcome("Plus de 2.5 buts", 1, 1))
        self.assertTrue(tracking.outcome("Moins de 2.5 buts", 1, 1))

    def test_an_unjudgeable_market_stays_open(self) -> None:
        """Le score final ne dit rien de la mi-temps : le pronostic reste non regle."""
        self.assertIsNone(tracking.outcome("Les deux marquent : oui (1re mi-temps)", 2, 1))
        untouched = tracking.settle(
            self._prediction(market="Les deux marquent : oui (1re mi-temps)"), 2, 1
        )
        self.assertFalse(untouched.settled)

    def test_settling_records_the_score(self) -> None:
        judged = tracking.settle(self._prediction(), 1, 1)
        self.assertEqual((judged.won, judged.score), (True, "1-1"))

    def test_results_are_matched_by_team_names(self) -> None:
        played = flashscore.PastMatch(
            date="25.07.2026", home="Equipe", away="Visiteur", home_goals=0, away_goals=0
        )
        updated, settled = tracking.settle_all([self._prediction()], lambda _team: [played])
        self.assertEqual(settled, 1)
        self.assertEqual((updated[0].won, updated[0].score), (False, "0-0"))

    def test_an_unfound_match_is_left_for_the_next_review(self) -> None:
        updated, settled = tracking.settle_all([self._prediction()], lambda _team: [])
        self.assertEqual(settled, 0)
        self.assertFalse(updated[0].settled)

    def test_each_source_is_measured_apart(self) -> None:
        """Forebet annonce 70 %, le modele 60 % : seul le resultat reel les separe."""
        settled = [
            tracking.settle(self._prediction(), 1, 1),
            tracking.settle(self._prediction(), 2, 0),
        ]
        by_source = {item.source: item for item in tracking.calibrations(settled)}
        self.assertEqual(by_source["Forebet"].announced, 70.0)
        self.assertEqual(by_source["Forebet"].realised, 50.0)
        self.assertEqual(by_source["Forebet"].bias, 20.0)
        self.assertLess(by_source["modele"].brier, by_source["Forebet"].brier)
        self.assertFalse(by_source["Forebet"].meaningful)

    def test_the_file_survives_a_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            bundles = [self._bundle()]
            path = tracking.record(bundles, output, day="2026-07-25")
            first = tracking.load(path)
            self.assertTrue(first)
            tracking.record(bundles, output, day="2026-07-26")
            self.assertEqual(len(tracking.load(path)), 2 * len(first))
            tracking.save([tracking.settle(first[0], 1, 1)], path)
            self.assertTrue(tracking.load(path)[0].settled)

    def test_the_review_warns_on_a_thin_sample(self) -> None:
        text = tracking.markdown([tracking.settle(self._prediction(), 1, 1)])
        self.assertIn("| Forebet |", text)
        self.assertIn(f"Moins de {tracking.MEANINGFUL_SAMPLE}", text)

    def test_nothing_settled_says_so(self) -> None:
        self.assertIn("Aucun pronostic regle", tracking.markdown([self._prediction()]))


class TestPriority(unittest.TestCase):
    """Choix des rencontres quand la journee depasse le plafond."""

    def _fixture(
        self, home: str, competition: str, goals: float | None = None
    ) -> ForebetPrediction:
        return ForebetPrediction(
            home_team=home, away_team="Adversaire", competition=competition, avg_goals=goals
        )

    def test_a_known_competition_passes_before_an_obscure_one(self) -> None:
        obscure = self._fixture("Inconnu", "Islande - 3e division", goals=4.0)
        major = self._fixture("Arsenal", "Angleterre - Premier League", goals=2.4)
        kept = priority.prioritise([obscure, major], 1)
        self.assertEqual([item.home_team for item in kept], ["Arsenal"])

    def test_within_a_tier_the_goals_decide(self) -> None:
        tight = self._fixture("Getafe", "Espagne - LaLiga", goals=1.9)
        open_game = self._fixture("Bayern", "Allemagne - Bundesliga", goals=3.6)
        kept = priority.prioritise([tight, open_game])
        self.assertEqual([item.home_team for item in kept], ["Bayern", "Getafe"])

    def test_over_25_replaces_a_missing_average(self) -> None:
        """Les pages par marche donnent le plus/moins 2.5 sans moyenne de buts."""
        prolific = self._fixture("A", "Angleterre - Premier League")
        prolific.markets["Plus de 2.5 buts"] = 75.0
        closed = self._fixture("B", "Angleterre - Premier League")
        closed.markets["Plus de 2.5 buts"] = 35.0
        self.assertGreater(priority.expected_goals(prolific), priority.expected_goals(closed))
        self.assertEqual(
            [item.home_team for item in priority.prioritise([closed, prolific])], ["A", "B"]
        )

    def test_an_unknown_competition_is_not_penalised_on_goals(self) -> None:
        """Sans moyenne publiee, la rencontre n'est ni avantagee ni ecartee."""
        self.assertIsNone(priority.expected_goals(self._fixture("A", "Coupe locale")))
        self.assertEqual(priority.competition_tier(None), priority.UNKNOWN_TIER)

    def test_the_order_of_the_source_breaks_ties(self) -> None:
        first = self._fixture("A", "France - Ligue 1", goals=2.5)
        second = self._fixture("B", "France - Ligue 1", goals=2.5)
        self.assertEqual(
            [item.home_team for item in priority.prioritise([first, second])], ["A", "B"]
        )


class TestTraps(unittest.TestCase):
    """Matchs pieges : confrontations directes, classement et solidite des defenses."""

    def _stats(self, **changes: object) -> MatchStats:
        base = MatchStats(
            home_team="Recevant",
            away_team="Visiteur",
            home_table=TableStanding(
                name="Recevant", position=5, played=20, draws=4, goals_for=30, goals_against=25
            ),
            away_table=TableStanding(
                name="Visiteur", position=12, played=20, draws=4, goals_for=30, goals_against=25
            ),
        )
        return replace(base, **changes)

    def test_two_solid_defences_condemn_both_teams_to_score(self) -> None:
        stats = self._stats(
            home_table=TableStanding(name="Recevant", position=1, played=20, goals_against=12),
            away_table=TableStanding(name="Visiteur", position=14, played=20, goals_against=16),
        )
        self.assertTrue(trap.is_trap(stats, BTTS_YES))
        self.assertFalse(trap.is_trap(stats, BTTS_NO))

    def test_both_teams_to_score_demands_two_real_attacks(self) -> None:
        """Le marche exige que les DEUX equipes marquent : la moins prolifique commande."""
        stats = self._stats(
            away_table=TableStanding(
                name="Visiteur", position=12, played=20, goals_for=16, goals_against=25
            )
        )
        reasons = trap.trap_reasons(stats, BTTS_YES)
        self.assertTrue(any("attaque de Visiteur" in reason for reason in reasons), reasons)
        # L'attaque du recevant, elle, tient le marche.
        self.assertFalse(any("attaque de Recevant" in reason for reason in reasons), reasons)
        # Deux attaques fournies ne declenchent rien.
        self.assertEqual(trap.trap_reasons(self._stats(), BTTS_YES), [])

    def test_a_leaky_defence_condemns_the_clean_sheet(self) -> None:
        stats = self._stats(
            away_table=TableStanding(name="Visiteur", position=18, played=20, goals_against=40)
        )
        self.assertTrue(trap.is_trap(stats, BTTS_NO))
        reasons = trap.trap_reasons(stats, BTTS_NO)
        self.assertIn("Visiteur", reasons[0])

    def test_head_to_head_contradicts_the_selection(self) -> None:
        closed = self._stats(
            head_to_head=["10.05. Recevant 0-0 Visiteur", "12.12. Visiteur 1-0 Recevant"]
        )
        self.assertTrue(trap.is_trap(closed, BTTS_YES))
        self.assertAlmostEqual(trap.head_to_head_goals(closed), 0.5)

    def test_a_date_is_not_a_score(self) -> None:
        """« 2026-03-02 » ne doit pas se lire comme un 2026-3."""
        dated = self._stats(
            head_to_head=[
                "2026-03-02 Visiteur 1-2 Recevant",
                "2025-10-19 Recevant 3-1 Visiteur",
            ]
        )
        self.assertAlmostEqual(trap.head_to_head_goals(dated), 3.5)

    def test_a_single_meeting_proves_nothing(self) -> None:
        self.assertIsNone(trap.head_to_head_goals(self._stats(head_to_head=["10.05. A 0-0 B"])))

    def test_neighbours_in_the_table_play_a_tense_match(self) -> None:
        stats = self._stats(
            home_table=TableStanding(name="Recevant", position=6, played=20, goals_against=25),
            away_table=TableStanding(name="Visiteur", position=7, played=20, goals_against=25),
        )
        self.assertTrue(trap.is_trap(stats, BTTS_YES))
        self.assertTrue(trap.is_trap(stats, "12"))

    def test_a_draw_prone_pair_makes_the_no_draw_fragile(self) -> None:
        stats = self._stats(
            home_table=TableStanding(name="Recevant", position=3, played=20, draws=9),
            away_table=TableStanding(name="Visiteur", position=15, played=20, draws=2),
        )
        self.assertTrue(trap.is_trap(stats, "12"))

    def test_a_better_placed_visitor_undermines_the_home_double_chance(self) -> None:
        stats = self._stats(
            home_table=TableStanding(name="Recevant", position=14, played=20, goals_against=25),
            away_table=TableStanding(name="Visiteur", position=2, played=20, goals_against=25),
        )
        self.assertTrue(trap.is_trap(stats, "1N"))
        self.assertFalse(trap.is_trap(stats, "N2"))

    def test_a_closed_predicted_score_kills_both_teams_to_score(self) -> None:
        """0-0, 1-0 et 0-1 sont les scores ou un seul but retourne le pronostic."""
        bare = MatchStats(home_team="A", away_team="B")
        for score in ("0-0", "1-0", "0-1"):
            self.assertTrue(trap.is_trap(bare, BTTS_YES, score), score)
            self.assertTrue(trap.is_trap(bare, "12", score), score)
        for score in ("2-1", "1-1", "3-0"):
            self.assertFalse(trap.is_trap(bare, BTTS_YES, score), score)

    def test_an_unreadable_predicted_score_says_nothing(self) -> None:
        self.assertFalse(trap.predicted_closed_game(None))
        self.assertFalse(trap.predicted_closed_game("?"))

    def test_nothing_to_say_without_flashscore_data(self) -> None:
        bare = MatchStats(home_team="A", away_team="B")
        self.assertEqual(
            [trap.trap_reasons(bare, market) for market in trap.TRAP_MARKETS], [[]] * 5
        )


class TestFlashscoreStandings(unittest.TestCase):
    """Lecture du classement Flashscore, sans reseau."""

    class _Cell:
        def __init__(self, text: str) -> None:
            self._text = text

        def inner_text(self) -> str:
            return self._text

    class _Page:
        def __init__(self, rows: list[str]) -> None:
            self._rows = rows

        def query_selector_all(self, selector: str) -> list[TestFlashscoreStandings._Cell]:
            assert selector == ".ui-table__row"
            return [TestFlashscoreStandings._Cell(row) for row in self._rows]

    def test_reads_rank_goals_and_points(self) -> None:
        page = self._Page(
            [
                "1.\nArsenal\n38\n26\n7\n5\n71:27\n44\n85\nW\nW",
                "2.\nManchester City\n38\n23\n9\n6\n77:35\n42\n78\nL\nD",
                "Classement complet",
            ]
        )
        table = flashscore._parse_standings(page)
        self.assertEqual([row.name for row in table], ["Arsenal", "Manchester City"])
        self.assertEqual((table[0].position, table[0].points), (1, 85))
        self.assertAlmostEqual(table[0].conceded_per_game, 27 / 38)
        self.assertAlmostEqual(table[1].draw_share, 9 / 38)

    def test_finds_a_team_by_resemblance(self) -> None:
        table = [TableStanding(name="Manchester City", position=2)]
        found = flashscore.standing_for("Man City", table)
        assert found is not None
        self.assertEqual(found.position, 2)
        self.assertIsNone(flashscore.standing_for("Arsenal", table))


class TestFlashscoreKickoff(unittest.TestCase):
    """Heure de coup d'envoi lue sur Flashscore, sans annee affichee."""

    def test_day_and_month_take_the_current_year(self) -> None:
        self.assertEqual(
            flashscore._kickoff_from("03.08. 15:30", today=date(2026, 7, 25)),
            "2026-08-03 15:30",
        )

    def test_january_seen_in_december_belongs_to_the_next_year(self) -> None:
        self.assertEqual(
            flashscore._kickoff_from("04.01. 20:45", today=date(2026, 12, 20)),
            "2027-01-04 20:45",
        )

    def test_a_played_match_has_no_kickoff(self) -> None:
        self.assertIsNone(flashscore._kickoff_from("Termine"))


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
