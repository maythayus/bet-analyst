#!/usr/bin/env bash
# Equivalent Linux / Omarchy de analyse.cmd : analyse toutes les rencontres du jour
# cotees chez Unibet et affiche le rapport dans le terminal.
#
# Si le fichier Forebet.htm est present a la racine du projet (page Forebet
# enregistree avec Ctrl+S puis renommee), ses pronostics sont utilises en plus.
#
#   ./analyse.sh                        analyse de la journee
#   ./analyse.sh --matches 20 --print   tes propres options, passees telles quelles

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# Le dossier du projet doit etre importable meme si Python ignore le repertoire courant.
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

if [[ -x .venv/bin/python ]]; then
    PYTHON=.venv/bin/python
else
    PYTHON=python3
fi

# Un venv fraichement cree n'a aucune dependance : le dire au lieu d'une pile d'erreurs.
if ! "$PYTHON" -c "import requests, bs4" 2>/dev/null; then
    echo "Dependances manquantes. Lance une fois : ./installer-linux.sh"
    exit 1
fi

if [[ $# -gt 0 ]]; then
    exec "$PYTHON" -m betbot "$@"
fi

if [[ -f Forebet.htm ]]; then
    echo "Forebet.htm trouve : pronostics Forebet + cotes Unibet du jour."
    exec "$PYTHON" -m betbot --forebet-html Forebet.htm --today --only-bettable \
        --combo 4 --combo-market "Les deux marquent : oui" --print
else
    echo "Pas de Forebet.htm : analyse des seules rencontres cotees chez Unibet."
    echo "Pour ajouter les pronostics Forebet, voir le README (Ctrl+S sur Forebet, fichier renomme Forebet.htm ici)."
    exec "$PYTHON" -m betbot --from-unibet --today \
        --combo 4 --combo-market "Les deux marquent : oui" --print
fi
