#!/usr/bin/env bash
# Installation sous Linux / Omarchy : environnement Python et dependances.
#
#   ./installer-linux.sh           venv + dependances
#   ./installer-linux.sh --timer   idem, plus l'analyse quotidienne (systemd, 8h00)
#
# Chromium : celui du systeme est utilise s'il est present (Omarchy l'installe
# d'office) ; sinon le navigateur de Playwright est telecharge.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade --quiet pip
.venv/bin/python -m pip install --quiet -r requirements.txt

if ! command -v chromium >/dev/null && ! command -v google-chrome-stable >/dev/null; then
    .venv/bin/python -m playwright install chromium
fi

[[ -f .env ]] || cp .env.exemple .env

if [[ "${1:-}" == "--timer" ]]; then
    units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$units"
    cat > "$units/betbot.service" <<UNIT
[Unit]
Description=Bet.Bot : analyse quotidienne des paris
After=network-online.target

[Service]
Type=oneshot
ExecStart=$PWD/analyse-quotidienne.sh
UNIT
    cat > "$units/betbot.timer" <<UNIT
[Unit]
Description=Bet.Bot : analyse quotidienne a 8h00

[Timer]
OnCalendar=*-*-* 08:00:00
# Machine eteinte a 8h00 : l'analyse part au demarrage suivant.
Persistent=true

[Install]
WantedBy=timers.target
UNIT
    systemctl --user daemon-reload
    systemctl --user enable --now betbot.timer
    echo "Minuteur actif : systemctl --user list-timers betbot.timer"
fi

echo "Pret. Essai sans reseau ni LLM : ./analyse.sh --demo --no-llm"
