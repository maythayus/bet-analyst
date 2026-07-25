"""Point d'entree en ligne de commande : `python -m betanalyst`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from betanalyst.config import AppConfig
from betanalyst.pipeline import run
from betanalyst.report import write_report
from betanalyst.sources.http import FetchError


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="betanalyst",
        description="Collecte Forebet + Flashscore, calcule un modele de Poisson, "
        "puis fait analyser le tout par un LLM local (LM Studio).",
    )
    parser.add_argument("--matches", type=int, default=None, help="nombre de matchs a analyser")
    parser.add_argument(
        "--match",
        action="append",
        default=None,
        metavar='"Equipe A vs Equipe B"',
        help="analyser une rencontre precise sans passer par le listing Forebet "
        "(option repetable)",
    )
    parser.add_argument("--model", default=None, help="identifiant du modele charge dans LM Studio")
    parser.add_argument("--base-url", default=None, help="URL de l'API LM Studio")
    parser.add_argument(
        "--temperature", type=float, default=None, help="temperature (0 recommande)"
    )
    parser.add_argument("--output", type=Path, default=None, help="dossier de sortie des rapports")
    parser.add_argument("--no-flashscore", action="store_true", help="ignorer Flashscore")
    parser.add_argument("--no-llm", action="store_true", help="rapport statistique seul, sans LLM")
    parser.add_argument("--no-cache", action="store_true", help="forcer le telechargement")
    parser.add_argument(
        "--forebet-html",
        type=Path,
        default=None,
        help="page Forebet sauvegardee a la main (contourne le blocage anti-bot)",
    )
    parser.add_argument(
        "--no-bookmakers", action="store_true", help="ne pas recuperer les cotes Unibet"
    )
    parser.add_argument(
        "--only-bettable",
        action="store_true",
        help="ne garder que les rencontres cotees chez un bookmaker",
    )
    parser.add_argument(
        "--odds-csv",
        type=Path,
        default=None,
        help="cotes saisies a la main (ParionsSport), format 'match;1;X;2'",
    )
    parser.add_argument(
        "--min-prob",
        type=float,
        default=None,
        metavar="PCT",
        help="probabilite Forebet minimale du pronostic, ex. 95",
    )
    parser.add_argument(
        "--min-odds",
        type=float,
        default=None,
        metavar="COTE",
        help="cote minimale sur le pronostic Forebet, ex. 1.5",
    )
    parser.add_argument("--demo", action="store_true", help="donnees fictives, aucun acces reseau")
    parser.add_argument("--verbose", "-v", action="store_true", help="logs detailles")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    cfg = AppConfig()
    if args.matches:
        cfg.scrape.max_matches = args.matches
    if args.model:
        cfg.lmstudio.model = args.model
    if args.base_url:
        cfg.lmstudio.base_url = args.base_url
    if args.temperature is not None:
        cfg.lmstudio.temperature = args.temperature
    if args.output:
        cfg.output_dir = args.output

    try:
        pairs = run(
            cfg,
            use_flashscore=not args.no_flashscore,
            use_llm=not args.no_llm,
            use_cache=not args.no_cache,
            offline=args.demo,
            forebet_html=args.forebet_html,
            matches=args.match,
            use_bookmakers=not args.no_bookmakers,
            only_bettable=args.only_bettable,
            odds_csv=args.odds_csv,
            min_probability=args.min_prob,
            min_odds=args.min_odds,
        )
    except FetchError as exc:
        print(f"Collecte impossible : {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"Fichier introuvable : {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Argument invalide : {exc}", file=sys.stderr)
        return 2

    if not pairs:
        print("Aucun match analyse.", file=sys.stderr)
        return 1

    markdown_path, json_path = write_report(pairs, cfg.output_dir)
    print(f"\nRapport  : {markdown_path}")
    print(f"Donnees  : {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
