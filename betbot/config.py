"""Configuration centrale (surchargeable par variables d'environnement)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Depuis Bet.Bot.exe, les sources vivent dans un dossier temporaire : les rapports doivent
# quand meme atterrir a cote de l'executable, la ou l'utilisateur ira les chercher.
PROJECT_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "out"
DEFAULT_CACHE_DIR = PROJECT_ROOT / ".cache"

# Dans l'executable, Playwright cherche Chromium dans son dossier temporaire, qui est
# recree a chaque lancement : on le renvoie vers l'emplacement standard de l'utilisateur.
if getattr(sys, "frozen", False):
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / "AppData" / "Local" / "ms-playwright")
    )


def _load_env_file(path: Path) -> None:
    """Charge un fichier .env pose a cote du projet, sans ecraser l'environnement.

    Les identifiants de messagerie et de WordPress n'ont rien a faire dans le depot ni
    dans une ligne de commande : ce fichier est ignore par Git.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        name, _, value = entry.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


_load_env_file(PROJECT_ROOT / ".env")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclass
class LMStudioConfig:
    """Parametres du serveur local LM Studio (API compatible OpenAI)."""

    base_url: str = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
    api_key: str = os.getenv("LMSTUDIO_API_KEY", "lm-studio")
    model: str = os.getenv("LMSTUDIO_MODEL", "deepseek-r1-distill-llama-8b")
    temperature: float = float(os.getenv("LMSTUDIO_TEMPERATURE", "0"))
    max_tokens: int = int(os.getenv("LMSTUDIO_MAX_TOKENS", "2048"))
    timeout: float = float(os.getenv("LMSTUDIO_TIMEOUT", "300"))


@dataclass
class ScrapeConfig:
    """Parametres de collecte."""

    forebet_url: str = os.getenv("FOREBET_URL", "https://www.forebet.com/en/football-tips-and-predictions-for-today")
    request_timeout: float = 30.0
    delay_between_requests: float = 2.0  # politesse envers les serveurs
    flashscore_headless: bool = os.getenv("FLASHSCORE_HEADLESS", "1") != "0"
    flashscore_timeout_ms: int = 30_000
    max_matches: int = int(os.getenv("MAX_MATCHES", "10"))
    cache_dir: Path = DEFAULT_CACHE_DIR
    cache_ttl_seconds: int = 3600


@dataclass
class MailConfig:
    """Envoi du rapport par courriel. Les valeurs par defaut visent Gmail.

    Gmail refuse le mot de passe du compte : il faut un mot de passe d'application
    (https://myaccount.google.com/apppasswords), a poser dans `.env` sous
    `BETBOT_MAIL_PASSWORD`.
    """

    host: str = os.getenv("BETBOT_MAIL_HOST", "smtp.gmail.com")
    port: int = int(os.getenv("BETBOT_MAIL_PORT", "587"))
    user: str = os.getenv("BETBOT_MAIL_USER", "")
    password: str = os.getenv("BETBOT_MAIL_PASSWORD", "")
    sender: str = os.getenv("BETBOT_MAIL_FROM", "")
    recipients: str = os.getenv("BETBOT_MAIL_TO", "")
    timeout: float = float(os.getenv("BETBOT_MAIL_TIMEOUT", "60"))


@dataclass
class WordPressConfig:
    """Publication du rapport en article WordPress, via l'API REST.

    L'authentification se fait avec un mot de passe d'application WordPress
    (Utilisateurs > Profil > Mots de passe d'application), jamais le mot de passe du
    compte. Les articles partent en brouillon : un pronostic se relit avant publication.
    """

    site: str = os.getenv("BETBOT_WP_SITE", "")
    user: str = os.getenv("BETBOT_WP_USER", "")
    password: str = os.getenv("BETBOT_WP_PASSWORD", "")
    status: str = os.getenv("BETBOT_WP_STATUS", "draft")
    categories: str = os.getenv("BETBOT_WP_CATEGORIES", "")


@dataclass
class AppConfig:
    lmstudio: LMStudioConfig = field(default_factory=LMStudioConfig)
    scrape: ScrapeConfig = field(default_factory=ScrapeConfig)
    mail: MailConfig = field(default_factory=MailConfig)
    wordpress: WordPressConfig = field(default_factory=WordPressConfig)
    output_dir: Path = DEFAULT_OUTPUT_DIR
    # Modele de buts : "forme" (celui d'origine) ou "marche" (cale sur les cotes).
    poisson_model: str = os.getenv("BETBOT_POISSON", "forme")
