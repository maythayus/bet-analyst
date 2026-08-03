@echo off
rem Analyse quotidienne sans surveillance : enregistre les pages Forebet du jour avec un
rem navigateur, analyse la journee, puis envoie le rapport (mail et WordPress selon .env).
rem
rem A brancher sur une tache planifiee Windows, par exemple tous les jours a 8h00 :
rem
rem   schtasks /create /tn "Bet.Bot" /tr "\"%~f0\"" /sc daily /st 08:00
rem
rem La toute premiere execution doit se faire a la main : Forebet peut afficher une
rem verification Cloudflare, a valider d'un clic dans la fenetre du navigateur. Le profil
rem garde ensuite les cookies.

setlocal
cd /d "%~dp0"
chcp 65001 >nul

set "PYTHONPATH=%~dp0;%PYTHONPATH%"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

rem --no-open : aucune fenetre de Bloc-notes a fermer sur une machine sans personne devant.
"%PYTHON%" -m betbot --save-forebet --from-unibet --today ^
    --combo 4 --combo-market "Les deux marquent : oui" --no-open

endlocal
