"""Orchestration : Forebet -> Flashscore -> Poisson -> LM Studio -> rapport."""

from __future__ import annotations

import logging
from pathlib import Path

from betbot import demo, poisson
from betbot.config import AppConfig
from betbot.llm import LMStudioClient, LMStudioError
from betbot.models import (
    Analysis,
    BookmakerLine,
    ForebetPrediction,
    MatchBundle,
    MatchStats,
)
from betbot.sources import bookmakers, flashscore, forebet
from betbot.sources.bookmakers import BookmakerOdds
from betbot.sources.flashscore import FlashscoreUnavailable

log = logging.getLogger(__name__)


def _collect_stats(
    prediction: ForebetPrediction, cfg: AppConfig, *, use_flashscore: bool, offline: bool
) -> MatchStats:
    if offline:
        fixture = demo.stats_for(prediction.home_team, prediction.away_team)
        if fixture:
            return fixture
    elif use_flashscore:
        try:
            stats = flashscore.fetch_match_stats(
                prediction.home_team,
                prediction.away_team,
                cfg.scrape,
                competition=prediction.competition,
            )
            stats.competition = stats.competition or prediction.competition
            stats.kickoff = stats.kickoff or prediction.kickoff
            return stats
        except (FlashscoreUnavailable, OSError, ValueError) as exc:
            log.warning("Flashscore indisponible pour %s : %s", prediction.home_team, exc)

    return MatchStats(
        home_team=prediction.home_team,
        away_team=prediction.away_team,
        kickoff=prediction.kickoff,
        competition=prediction.competition,
    )


def collect_odds(
    cfg: AppConfig, *, odds_csv: Path | None = None, today_only: bool = False
) -> list[BookmakerOdds]:
    """Cotes Unibet (API publique) completees par un fichier manuel (ParionsSport)."""
    entries: list[BookmakerOdds] = []
    try:
        entries.extend(bookmakers.fetch_unibet(cfg.scrape, today_only=today_only))
    except (OSError, ValueError) as exc:
        log.warning("Cotes Unibet indisponibles : %s", exc)
    if odds_csv:
        entries.extend(bookmakers.load_csv(odds_csv))
    return entries


def predictions_from_odds(entries: list[BookmakerOdds], limit: int) -> list[ForebetPrediction]:
    """Rencontres a analyser deduites des grilles bookmakers, sans passer par Forebet.

    Utile quand la page Forebet enregistree ne recoupe pas la fenetre couverte par le
    bookmaker : les matchs viennent alors des cotes, donc ils sont pariables par
    construction. Aucune probabilite Forebet n'est disponible dans ce mode.
    """
    predictions: list[ForebetPrediction] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (bookmakers.normalise(entry.home_team), bookmakers.normalise(entry.away_team))
        if key in seen:
            continue
        seen.add(key)
        predictions.append(
            ForebetPrediction(
                home_team=entry.home_team,
                away_team=entry.away_team,
                kickoff=entry.kickoff,
                competition=entry.competition,
            )
        )
        if len(predictions) >= limit:
            break
    return predictions


def enrich_with_detailed_markets(
    predictions: list[ForebetPrediction], entries: list[BookmakerOdds], cfg: AppConfig
) -> None:
    """Complete les cotes des rencontres retenues avec leur page detail Unibet.

    Le listing ne donne que le 1 N 2 ; la page de chaque rencontre expose aussi « les
    deux equipes marquent », les doubles chances et leurs combinaisons. Une requete
    par rencontre retenue, d'ou l'appel apres le filtrage.
    """
    for entry in entries:
        if not entry.url:
            continue
        if any(
            bookmakers.fixture_matches(entry, prediction.home_team, prediction.away_team)
            for prediction in predictions
        ):
            entry.odds.update(bookmakers.fetch_event_markets(entry, cfg.scrape))


def merge_forebet_markets(
    predictions: list[ForebetPrediction], extra: list[ForebetPrediction]
) -> None:
    """Ajoute aux rencontres les probabilites des pages Forebet par marche.

    Les noms d'equipes de ces pages sont ceux de Forebet, pas ceux du bookmaker :
    l'appariement passe par la comparaison tolerante deja utilisee pour les cotes.
    """
    for prediction in predictions:
        for candidate in extra:
            if bookmakers.teams_match(
                candidate.home_team, prediction.home_team
            ) and bookmakers.teams_match(candidate.away_team, prediction.away_team):
                prediction.markets.update(candidate.markets)
                break
        else:
            log.info(
                "Forebet par marche : rien pour %s vs %s",
                prediction.home_team,
                prediction.away_team,
            )


def _lines_for(
    prediction: ForebetPrediction, entries: list[BookmakerOdds]
) -> list[BookmakerLine]:
    lines: list[BookmakerLine] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.bookmaker in seen:
            continue
        if bookmakers.fixture_matches(entry, prediction.home_team, prediction.away_team):
            lines.append(BookmakerLine(bookmaker=entry.bookmaker, odds=entry.odds))
            seen.add(entry.bookmaker)
    return lines


def build_bundles(
    predictions: list[ForebetPrediction],
    cfg: AppConfig,
    *,
    use_flashscore: bool = True,
    offline: bool = False,
    odds: list[BookmakerOdds] | None = None,
) -> list[MatchBundle]:
    """Assemble, pour chaque match, stats + prediction Forebet + Poisson + cotes."""
    bundles: list[MatchBundle] = []
    for prediction in predictions:
        stats = _collect_stats(prediction, cfg, use_flashscore=use_flashscore, offline=offline)
        bundle = MatchBundle(
            stats=stats,
            forebet=prediction,
            bookmakers=_lines_for(prediction, odds) if odds else [],
        )
        # Les cotes servent de reference au modele : elles doivent donc etre attachees
        # a la rencontre avant le calcul.
        bundle.poisson = poisson.compute(stats, bundle.market_prices())
        bundles.append(bundle)
    return bundles


def filter_predictions(
    predictions: list[ForebetPrediction],
    entries: list[BookmakerOdds],
    *,
    only_bettable: bool,
    min_probability: float | None,
    min_odds: float | None,
    odds_range: tuple[float, float] | None = None,
) -> list[ForebetPrediction]:
    """Ne garde que les rencontres cotees et conformes aux seuils demandes."""
    kept: list[ForebetPrediction] = []
    for prediction in predictions:
        lines = _lines_for(prediction, entries)
        if only_bettable and not lines:
            log.info("Ecarte (non cote chez les bookmakers) : %s", prediction.home_team)
            continue

        if odds_range:
            low, high = odds_range
            in_range = any(low <= value <= high for line in lines for value in line.odds.values())
            if not in_range:
                continue

        probability = prediction.best_probability
        if min_probability is not None and (probability is None or probability < min_probability):
            continue

        if min_odds is not None:
            sign = prediction.pick
            available = [line.odds.get(sign) for line in lines if sign] if sign else []
            best = max([value for value in available if value], default=None)
            if best is None or best < min_odds:
                continue

        kept.append(prediction)
    return kept


def analyse_bundles(
    bundles: list[MatchBundle], cfg: AppConfig, *, use_llm: bool = True
) -> list[tuple[MatchBundle, Analysis | None]]:
    """Envoie chaque match au LLM local ; degrade sans bloquer si LM Studio est absent."""
    if not use_llm:
        return [(bundle, None) for bundle in bundles]

    client = LMStudioClient(cfg.lmstudio)
    try:
        available = client.list_models()
        if available and cfg.lmstudio.model not in available:
            log.warning(
                "Modele '%s' non charge dans LM Studio. Modeles disponibles : %s",
                cfg.lmstudio.model,
                ", ".join(available),
            )
    except LMStudioError as exc:
        log.error("%s", exc)
        return [(bundle, None) for bundle in bundles]

    results: list[tuple[MatchBundle, Analysis | None]] = []
    for index, bundle in enumerate(bundles, start=1):
        log.info("[%d/%d] Analyse de %s", index, len(bundles), bundle.label)
        try:
            results.append((bundle, client.analyse(bundle)))
        except LMStudioError as exc:
            log.error("Analyse impossible pour %s : %s", bundle.label, exc)
            results.append((bundle, None))
    return results


def parse_match_argument(text: str) -> ForebetPrediction:
    """Transforme "Lyon vs Rennes" (ou "Lyon - Rennes") en rencontre a analyser."""
    for separator in (" vs ", " VS ", " - ", " contre "):
        if separator in text:
            home, away = text.split(separator, 1)
            return ForebetPrediction(home_team=home.strip(), away_team=away.strip())
    raise ValueError(f"Format attendu 'Equipe A vs Equipe B', recu : {text!r}")


def run(
    cfg: AppConfig,
    *,
    use_flashscore: bool = True,
    use_llm: bool = True,
    use_cache: bool = True,
    offline: bool = False,
    forebet_html: Path | None = None,
    forebet_market_html: list[Path] | None = None,
    matches: list[str] | None = None,
    use_bookmakers: bool = True,
    only_bettable: bool = False,
    odds_csv: Path | None = None,
    min_probability: float | None = None,
    min_odds: float | None = None,
    odds_range: tuple[float, float] | None = None,
    detailed_odds: bool = True,
    from_bookmakers: bool = False,
    today_only: bool = False,
) -> list[tuple[MatchBundle, Analysis | None]]:
    odds = (
        collect_odds(cfg, odds_csv=odds_csv, today_only=today_only)
        if use_bookmakers and not offline
        else []
    )

    if matches:
        predictions = [parse_match_argument(text) for text in matches]
    elif offline:
        predictions = demo.predictions()
    elif from_bookmakers:
        predictions = predictions_from_odds(odds, cfg.scrape.max_matches)
        log.info("%d rencontres cotees a analyser", len(predictions))
    else:
        predictions = forebet.fetch_predictions(
            cfg.scrape, use_cache=use_cache, html_file=forebet_html
        )
    if not predictions:
        log.warning("Aucune rencontre a analyser (source vide ou structure modifiee ?)")
        return []

    if forebet_market_html:
        merge_forebet_markets(predictions, forebet.read_market_pages(forebet_market_html))

    if odds:
        before = len(predictions)
        predictions = filter_predictions(
            predictions,
            odds,
            only_bettable=only_bettable,
            min_probability=min_probability,
            min_odds=min_odds,
            odds_range=odds_range,
        )
        log.info("%d rencontres retenues sur %d apres filtrage", len(predictions), before)
        if not predictions:
            log.warning("Aucune rencontre ne passe les filtres demandes")
            return []
        if detailed_odds:
            enrich_with_detailed_markets(predictions, odds, cfg)

    bundles = build_bundles(
        predictions, cfg, use_flashscore=use_flashscore, offline=offline, odds=odds
    )
    if odds_range:
        bundles.sort(
            key=lambda bundle: max(
                (item[3] for item in bundle.opportunities(odds_range)), default=float("-inf")
            ),
            reverse=True,
        )
    return analyse_bundles(bundles, cfg, use_llm=use_llm)
