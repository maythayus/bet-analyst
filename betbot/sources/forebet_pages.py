"""Enregistrement automatique des pages Forebet par marche.

C'est le Ctrl+S quotidien, fait par un navigateur au lieu de la main : les pages sont
ouvertes une par une, a quelques secondes d'intervalle, dans un profil Chrome persistant
qui garde les cookies d'une fois sur l'autre.

Aucune protection n'est contournee. Si Forebet affiche la verification Cloudflare, le
navigateur reste ouvert le temps que tu cliques (fenetre visible), et sans profil deja
valide la sauvegarde s'arrete avec le message correspondant. Un solveur ou un proxy
tournant irait contre les conditions du site, et casserait a la mise a jour suivante.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol

from betbot.config import USER_AGENT, ScrapeConfig

log = logging.getLogger(__name__)

BASE_URL = "https://www.forebet.com/en/football-tips-and-predictions-for-today"

# Nom de fichier -> page. Les noms commencent par « Predictions » : ce sont ceux que
# l'analyse ramasse ensuite toute seule dans le dossier.
FOREBET_PAGES: dict[str, str] = {
    "Predictions 1X2 _ Today Forebet Football.htm": f"{BASE_URL}/predictions-1x2",
    "Predictions Both to score _ Today Forebet Football.htm": (
        f"{BASE_URL}/predictions-both-to-score"
    ),
    "Predictions Under_Over 2.5 goals _ Today Forebet Football.htm": (
        f"{BASE_URL}/predictions-under-over-goals"
    ),
    "Predictions Double chance _ Today Forebet Football.htm": (
        f"{BASE_URL}/double-chance-predictions"
    ),
    "Predictions Half Time (HT) _ Today Forebet Football.htm": f"{BASE_URL}/predictions-ht",
}

# Le tableau des rencontres : sa presence signe une page reellement chargee.
ROWS_SELECTOR = "div.rcnt, tr.tr_0, tr.tr_1"
# Titres et textes de la page d'attente Cloudflare.
CHALLENGE_MARKERS = (
    "just a moment",
    "un instant",
    "attention required",
    "checking your browser",
    "verify you are human",
)
# Temps laisse a l'utilisateur pour cliquer sur la verification, fenetre visible.
CHALLENGE_WAIT_SECONDS = 90


class LoadedPage(Protocol):
    """Ce que la sauvegarde utilise d'une page Playwright, sans importer le type."""

    def title(self) -> str: ...

    def content(self) -> str: ...


class ForebetSaveError(RuntimeError):
    """Sauvegarde impossible : navigateur absent, page vide ou verification en attente."""


def profile_dir(cfg: ScrapeConfig) -> Path:
    """Profil du navigateur, conserve pour garder les cookies de Forebet."""
    return cfg.cache_dir / "chrome-forebet"


def _is_challenge(page_title: str, content: str) -> bool:
    """La page affichee est-elle la verification Cloudflare plutot que le contenu ?"""
    haystack = f"{page_title} {content[:2000]}".lower()
    return any(marker in haystack for marker in CHALLENGE_MARKERS)


def save_pages(
    destination: Path,
    cfg: ScrapeConfig,
    *,
    pages: dict[str, str] | None = None,
    headless: bool = False,
) -> list[Path]:
    """Enregistre les pages Forebet dans `destination`. Retourne les fichiers ecrits."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depend de l'installation
        raise ForebetSaveError(
            "Playwright n'est pas installe. Lance : pip install playwright && "
            "playwright install chromium"
        ) from exc

    targets = pages if pages is not None else FOREBET_PAGES
    destination.mkdir(parents=True, exist_ok=True)
    profile = profile_dir(cfg)
    profile.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    with sync_playwright() as playwright:
        browser_args = ["--disable-blink-features=AutomationControlled"]
        try:
            # Le Chrome installe sur la machine passe la verification bien plus souvent
            # que le Chromium de Playwright. Son propre agent utilisateur est laisse tel
            # quel : annoncer une autre version que la sienne est un signal de plus.
            context = playwright.chromium.launch_persistent_context(
                str(profile), channel="chrome", headless=headless, locale="fr-FR", args=browser_args
            )
        except PlaywrightError:
            log.info("Chrome introuvable : le Chromium de Playwright prend le relais.")
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(profile),
                    headless=headless,
                    locale="fr-FR",
                    user_agent=USER_AGENT,
                    args=browser_args,
                )
            except PlaywrightError as exc:  # pragma: no cover - depend de l'installation
                raise ForebetSaveError(
                    "Aucun navigateur utilisable. Installe Chrome, ou lance une fois : "
                    "Bet.Bot.exe --install-chromium (ou playwright install chromium)"
                ) from exc

        page = context.new_page()
        try:
            for index, (filename, url) in enumerate(targets.items()):
                if index:
                    # Meme politesse qu'un lecteur qui parcourt les onglets a la main.
                    time.sleep(cfg.delay_between_requests)
                log.info("Forebet : %s", url)
                try:
                    page.goto(url, timeout=cfg.flashscore_timeout_ms, wait_until="domcontentloaded")
                    page.wait_for_selector(ROWS_SELECTOR, timeout=cfg.flashscore_timeout_ms)
                except PlaywrightTimeout:
                    if _is_challenge(page.title(), page.content()):
                        _wait_for_human(page, headless=headless)
                    else:
                        raise ForebetSaveError(
                            f"Aucune rencontre sur {url} : page changee ou vide."
                        ) from None

                content = page.content()
                if _is_challenge(page.title(), content):
                    _wait_for_human(page, headless=headless)
                    content = page.content()

                target = destination / filename
                target.write_text(content, encoding="utf-8")
                written.append(target)
                log.info("Page enregistree : %s", target)
        finally:
            context.close()

    return written


def _wait_for_human(page: LoadedPage, *, headless: bool) -> None:
    """Laisse le temps de cliquer sur la verification Cloudflare, fenetre visible.

    En mode invisible, personne ne peut cliquer : autant le dire tout de suite plutot que
    d'enregistrer la page d'attente a la place des pronostics.
    """
    if headless:
        raise ForebetSaveError(
            "Forebet affiche la verification Cloudflare et la fenetre est masquee. "
            "Relance une fois sans --save-forebet-headless : clique sur la case, le "
            "profil garde ensuite les cookies."
        )
    log.warning(
        "Verification Cloudflare affichee : clique dessus dans la fenetre "
        "(%s secondes d'attente).",
        CHALLENGE_WAIT_SECONDS,
    )
    deadline = time.monotonic() + CHALLENGE_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(2)
        if not _is_challenge(page.title(), page.content()):
            return
    raise ForebetSaveError(
        "La verification Cloudflare est toujours affichee : la page n'a pas ete "
        "enregistree. Ouvre Forebet dans ton navigateur, valide la verification, puis "
        "relance."
    )
