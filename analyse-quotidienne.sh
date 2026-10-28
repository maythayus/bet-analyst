#!/usr/bin/env bash
# Equivalent Linux / Omarchy de analyse-quotidienne.cmd : enregistre les pages Forebet du
# jour avec un navigateur, analyse la journee, puis envoie le rapport (mail et WordPress
# selon .env). Une notification de bureau annonce la fin, ou l'echec.
#
# Lance chaque jour par le minuteur systemd que pose ./installer-linux.sh --timer.
#
# La toute premiere execution doit se faire a la main : Forebet peut afficher une
# verification Cloudflare, a valider d'un clic dans la fenetre du navigateur. Le profil
# garde ensuite les cookies.

set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"

export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

if [[ -x .venv/bin/python ]]; then
    PYTHON=.venv/bin/python
else
    PYTHON=python3
fi

notify() {
    command -v notify-send >/dev/null && notify-send --app-name=Bet.Bot "$@" || true
}

# --no-open : aucune fenetre a fermer sur une machine sans personne devant.
if "$PYTHON" -m betbot --save-forebet --from-unibet --today \
    --combo 4 --combo-market "Les deux marquent : oui" --no-open; then
    latest=$(ls -t out/rapport-*.md 2>/dev/null | head -n 1)
    notify "Bet.Bot : analyse du jour prete" "${latest:-out/}"
else
    status=$?
    notify --urgency=critical "Bet.Bot : analyse en echec" "journalctl --user -u betbot"
    exit "$status"
fi
