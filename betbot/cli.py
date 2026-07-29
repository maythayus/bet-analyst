"""Point d'entree en ligne de commande : `python -m betbot`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from betbot.combo import build_ticket
from betbot.config import AppConfig
from betbot.pipeline import run
from betbot.report import build_markdown, write_report
from betbot.sources.http import FetchError

# Avec --today, la journee entiere est analysee : ce plafond n'existe que pour eviter
# une boucle sans fin si Unibet renvoyait un listing aberrant.
ALL_MATCHES = 500


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="betbot",
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
    parser.add_argument(
        "--odds-range",
        type=float,
        nargs=2,
        default=None,
        metavar=("MIN", "MAX"),
        help="ne garder que les matchs offrant une cote dans cette fourchette, "
        "ex. --odds-range 1.65 1.95 ; les matchs sont tries par valeur decroissante",
    )
    parser.add_argument(
        "--from-unibet",
        action="store_true",
        help="analyser les prochaines rencontres cotees chez Unibet au lieu de la liste "
        "Forebet : les cotes sont alors garanties, mais sans pronostic Forebet",
    )
    parser.add_argument(
        "--today",
        action="store_true",
        help="toutes les rencontres du jour cotees chez Unibet, et non les 50 prochaines "
        "(sans --matches, elles sont toutes analysees : comptez plusieurs minutes)",
    )
    parser.add_argument(
        "--no-detailed-odds",
        action="store_true",
        help="ne pas ouvrir la page Unibet de chaque rencontre retenue "
        "(plus rapide, mais pas de cote pour les marches combines)",
    )
    parser.add_argument(
        "--combo",
        type=int,
        default=None,
        metavar="N",
        help="construire un ticket combine avec les N selections les plus probables",
    )
    parser.add_argument(
        "--combo-market",
        default=None,
        metavar="MARCHE",
        help="imposer le meme marche a toutes les selections, "
        "ex. --combo-market \"Les deux marquent : oui\"",
    )
    parser.add_argument(
        "--min-combo-prob",
        type=float,
        default=None,
        metavar="PCT",
        help="probabilite minimale que le ticket passe ; les selections les moins "
        "probables sont retirees jusqu'a atteindre ce seuil, ex. 25",
    )
    parser.add_argument(
        "--print",
        dest="print_report",
        action="store_true",
        help="afficher le rapport dans la console en plus de l'ecrire dans out/",
    )
    parser.add_argument("--demo", action="store_true", help="donnees fictives, aucun acces reseau")
    parser.add_argument("--verbose", "-v", action="store_true", help="logs detailles")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # La console Windows est en cp1252 : sans cela, les accents du rapport la font planter.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    cfg = AppConfig()
    if args.matches:
        cfg.scrape.max_matches = args.matches
    elif args.today:
        cfg.scrape.max_matches = ALL_MATCHES
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
            odds_range=tuple(args.odds_range) if args.odds_range else None,
            detailed_odds=not args.no_detailed_odds,
            from_bookmakers=args.from_unibet,
            today_only=args.today,
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

    ticket = None
    if args.combo or args.min_combo_prob:
        ticket = build_ticket(
            [bundle for bundle, _ in pairs],
            legs=args.combo or 4,
            market=args.combo_market,
            min_probability=args.min_combo_prob,
        )
        if ticket is None:
            print(
                "Aucun ticket ne depasse la probabilite demandee : meme reduit a deux "
                "selections, le combine reste en dessous du seuil.",
                file=sys.stderr,
            )
        else:
            deadline = f", a valider avant {ticket.deadline}" if ticket.deadline else ""
            print(
                f"\nTicket {len(ticket.legs)} selections : "
                f"{ticket.probability:.2f} % de chances (1 fois sur {ticket.one_in}), "
                f"cote minimale a exiger {ticket.fair_odds:.2f}{deadline}"
            )

    markdown_path, json_path = write_report(pairs, cfg.output_dir, ticket)
    if args.print_report:
        print()
        print(build_markdown(pairs, ticket))
    print(f"\nRapport  : {markdown_path}")
    print(f"Donnees  : {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
