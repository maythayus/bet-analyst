"""Utilitaire HTTP avec cache disque et repli navigateur (anti-403)."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import requests

from betbot.config import USER_AGENT, ScrapeConfig

log = logging.getLogger(__name__)

BLOCKED_STATUS = {403, 429, 503}
CHALLENGE_MARKERS = ("Un instant", "Just a moment", "Checking your browser", "cf-challenge")


class FetchError(RuntimeError):
    """La page n'a pas pu etre recuperee, ni en HTTP direct ni via le navigateur."""


def _cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{digest}.html"


def _fetch_with_requests(url: str, cfg: ScrapeConfig) -> str:
    response = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=cfg.request_timeout,
    )
    response.raise_for_status()
    time.sleep(cfg.delay_between_requests)
    return response.text


def _is_challenge(html: str) -> bool:
    head = html[:4000]
    return any(marker in head for marker in CHALLENGE_MARKERS)


def fetch_with_browser(url: str, cfg: ScrapeConfig, *, wait_selector: str | None = None) -> str:
    """Recupere la page via Chromium (Playwright).

    Le profil du navigateur est persistant : une fois le controle Cloudflare passe,
    le cookie `cf_clearance` est reutilise lors des executions suivantes.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise FetchError(
            f"{url} a refuse la requete HTTP directe et Playwright n'est pas installe. "
            "Lance : pip install playwright && playwright install chromium"
        ) from exc

    profile_dir = cfg.cache_dir / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    log.info("Repli navigateur sur %s", url)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=cfg.flashscore_headless,
            user_agent=USER_AGENT,
            locale="fr-FR",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url, timeout=cfg.flashscore_timeout_ms, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=cfg.flashscore_timeout_ms)
                except PlaywrightTimeout:
                    log.warning("Selecteur '%s' absent apres attente", wait_selector)
            else:
                page.wait_for_timeout(3000)

            html = page.content()
        except PlaywrightError as exc:
            raise FetchError(
                f"Navigateur ferme avant la fin du chargement de {url}. "
                "Laisse la fenetre ouverte le temps que la page des pronostics s'affiche."
            ) from exc
        else:
            if _is_challenge(html):
                raise FetchError(
                    f"{url} affiche un controle anti-bot Cloudflare. "
                    "Enregistre la page depuis ton navigateur (Ctrl+S) et relance avec "
                    "--forebet-html \"chemin\\vers\\page.htm\", ou passe par "
                    "--match \"Equipe A vs Equipe B\"."
                )
            return html
        finally:
            context.close()


def fetch_html(
    url: str, cfg: ScrapeConfig, *, use_cache: bool = True, wait_selector: str | None = None
) -> str:
    """Recupere une page HTML : cache local, puis requests, puis navigateur headless."""
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cfg.cache_dir, url)

    if use_cache and path.exists():
        age = time.time() - path.stat().st_mtime
        if age < cfg.cache_ttl_seconds:
            log.debug("cache hit (%.0fs) pour %s", age, url)
            return path.read_text(encoding="utf-8", errors="replace")

    log.info("GET %s", url)
    try:
        html = _fetch_with_requests(url, cfg)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status not in BLOCKED_STATUS:
            raise FetchError(f"{url} : HTTP {status}") from exc
        log.warning("HTTP %s sur %s, tentative via navigateur", status, url)
        html = fetch_with_browser(url, cfg, wait_selector=wait_selector)
    except requests.RequestException as exc:
        raise FetchError(f"{url} injoignable : {exc}") from exc

    path.write_text(html, encoding="utf-8", errors="replace")
    return html
