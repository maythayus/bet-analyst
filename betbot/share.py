"""Diffusion du rapport : courriel et publication WordPress.

Le rapport reste ecrit sur le disque dans tous les cas ; ces deux canaux ne font que
l'expedier. Un echec de diffusion n'invalide pas l'analyse, il est signale et le run se
termine normalement.
"""

from __future__ import annotations

import logging
import mimetypes
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

import requests

from betbot.config import MailConfig, WordPressConfig

log = logging.getLogger(__name__)

MAIL_INTRO = (
    "Rapport Bet.Bot en piece jointe (Markdown et JSON). Les probabilites sont des "
    "estimations, jamais une garantie : le pari sportif est perdant a long terme du "
    "fait de la marge du bookmaker. Aide : 09 74 75 13 13."
)


class ShareError(RuntimeError):
    """Diffusion impossible : identifiants manquants, serveur injoignable, refus."""


def _inline(text: str) -> str:
    """Gras, italique et code d'une ligne Markdown, une fois le HTML echappe."""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
    return re.sub(r"(?<![\w*])_([^_]+)_(?![\w*])", r"<em>\1</em>", escaped)


def _table_row(line: str, cell: str) -> str:
    cells = [_inline(value.strip()) for value in line.strip().strip("|").split("|")]
    return "<tr>" + "".join(f"<{cell}>{value}</{cell}>" for value in cells) + "</tr>"


def markdown_to_html(markdown: str) -> str:
    """Convertit le rapport en HTML : titres, tableaux, citations, listes, paragraphes.

    Le rapport n'utilise qu'une poignee de constructions Markdown, toutes produites par
    `report.py` : une dependance de plus pour les couvrir serait disproportionnee.
    """
    html: list[str] = []
    table: list[str] = []
    paragraph: list[str] = []

    def close_paragraph() -> None:
        if paragraph:
            html.append(f"<p>{' '.join(paragraph)}</p>")
            paragraph.clear()

    def close_table() -> None:
        if table:
            html.append("<table>" + "".join(table) + "</table>")
            table.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("|"):
            close_paragraph()
            if set(line.replace("|", "").replace(" ", "")) <= {"-", ":"}:
                continue  # ligne de separation d'en-tete
            table.append(_table_row(line, "th" if not table else "td"))
            continue
        close_table()

        if not line:
            close_paragraph()
        elif line.startswith("#"):
            close_paragraph()
            level = min(len(line) - len(line.lstrip("#")), 6)
            html.append(f"<h{level}>{_inline(line.lstrip('# '))}</h{level}>")
        elif line.startswith(">"):
            close_paragraph()
            html.append(f"<blockquote><p>{_inline(line.lstrip('> '))}</p></blockquote>")
        elif line.startswith("---"):
            close_paragraph()
            html.append("<hr>")
        elif line.startswith(("- ", "* ")):
            close_paragraph()
            html.append(f"<ul><li>{_inline(line[2:])}</li></ul>")
        else:
            paragraph.append(_inline(line))

    close_paragraph()
    close_table()
    return "\n".join(html)


def _attach(message: EmailMessage, path: Path) -> None:
    kind, _ = mimetypes.guess_type(path.name)
    maintype, _, subtype = (kind or "application/octet-stream").partition("/")
    message.add_attachment(
        path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
    )


def recipients_of(cfg: MailConfig, override: str | None = None) -> list[str]:
    """Destinataires, separes par des virgules dans la configuration."""
    listed = (override or cfg.recipients).split(",")
    return [address.strip() for address in listed if address.strip()]


def send_report(cfg: MailConfig, markdown_path: Path, attachments: list[Path], to: str) -> None:
    """Envoie le rapport par SMTP, corps en HTML et fichiers en pieces jointes."""
    addresses = recipients_of(cfg, to)
    if not addresses:
        raise ShareError("Aucun destinataire : renseigne --mail-to ou BETBOT_MAIL_TO.")
    if not cfg.user or not cfg.password:
        raise ShareError(
            "Identifiants SMTP manquants : renseigne BETBOT_MAIL_USER et "
            "BETBOT_MAIL_PASSWORD dans le fichier .env. Avec Gmail, il faut un mot de "
            "passe d'application (https://myaccount.google.com/apppasswords)."
        )

    markdown = markdown_path.read_text(encoding="utf-8")
    title = markdown.splitlines()[0].lstrip("# ").strip() or markdown_path.stem

    message = EmailMessage()
    message["Subject"] = title
    message["From"] = cfg.sender or cfg.user
    message["To"] = ", ".join(addresses)
    message.set_content(f"{MAIL_INTRO}\n\n{markdown}")
    message.add_alternative(
        f"<html><body><p>{MAIL_INTRO}</p><hr>{markdown_to_html(markdown)}</body></html>",
        subtype="html",
    )
    for path in attachments:
        _attach(message, path)

    try:
        if cfg.port == 465:
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout) as server:
                server.login(cfg.user, cfg.password)
                server.send_message(message)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout) as server:
                server.starttls()
                server.login(cfg.user, cfg.password)
                server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise ShareError(
            f"{cfg.host} a refuse les identifiants : {exc}. Un mot de passe d'application "
            "(16 lettres minuscules, jamais celui du compte) se cree sur "
            "https://myaccount.google.com/apppasswords"
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise ShareError(f"Envoi impossible via {cfg.host}:{cfg.port} : {exc}") from exc

    log.info("Rapport envoye a %s", ", ".join(addresses))


def publish_report(cfg: WordPressConfig, markdown_path: Path, *, timeout: float = 60.0) -> str:
    """Publie le rapport en article WordPress et retourne son URL d'edition."""
    if not cfg.site or not cfg.user or not cfg.password:
        raise ShareError(
            "Identifiants WordPress manquants : renseigne BETBOT_WP_SITE, "
            "BETBOT_WP_USER et BETBOT_WP_PASSWORD dans le fichier .env. Le mot de passe "
            "est un mot de passe d'application (Utilisateurs > Profil)."
        )

    markdown = markdown_path.read_text(encoding="utf-8")
    title = markdown.splitlines()[0].lstrip("# ").strip() or markdown_path.stem
    payload: dict[str, object] = {
        "title": title,
        "content": markdown_to_html(markdown),
        "status": cfg.status,
    }
    categories = [int(value) for value in cfg.categories.split(",") if value.strip().isdigit()]
    if categories:
        payload["categories"] = categories

    site = cfg.site.rstrip("/")
    endpoint = f"{site}/wp-json/wp/v2/posts"
    try:
        response = requests.post(
            endpoint, json=payload, auth=(cfg.user, cfg.password), timeout=timeout
        )
    except requests.RequestException as exc:
        raise ShareError(f"WordPress injoignable ({endpoint}) : {exc}") from exc
    if response.status_code in (401, 403):
        raise ShareError(
            f"WordPress a refuse les identifiants ({response.status_code}) : "
            f"{response.text[:200]}. BETBOT_WP_USER est l'identifiant de connexion, et "
            "BETBOT_WP_PASSWORD un mot de passe d'application (24 caracteres en six "
            f"groupes), tous deux lisibles sur {site}/wp-admin/profile.php"
        )
    if response.status_code >= 400:
        raise ShareError(
            f"WordPress a refuse l'article ({response.status_code}) : {response.text[:200]}"
        )

    article = response.json()
    link = article.get("link") or endpoint
    log.info("Article WordPress cree (%s) : %s", cfg.status, link)
    return link
