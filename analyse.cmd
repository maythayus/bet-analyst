@echo off
rem Lanceur double-cliquable : analyse toutes les rencontres du jour cotees chez Unibet
rem et affiche le rapport dans la console.
rem
rem Si le fichier Forebet.htm est present a la racine du projet (page Forebet
rem enregistree avec Ctrl+S puis renommee), ses pronostics sont utilises en plus.
rem
rem   analyse.cmd                        analyse de la journee
rem   analyse.cmd --matches 20 --print   tes propres options, passees telles quelles

setlocal
cd /d "%~dp0"
chcp 65001 >nul

rem Le dossier du projet doit etre importable meme si Python ignore le repertoire courant.
set "PYTHONPATH=%~dp0;%PYTHONPATH%"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

if not "%~1"=="" (
    "%PYTHON%" -m betbot %*
    goto :fin
)

if exist "Forebet.htm" (
    echo Forebet.htm trouve : pronostics Forebet + cotes Unibet du jour.
    "%PYTHON%" -m betbot --forebet-html "Forebet.htm" --today --only-bettable --combo 4 --combo-market "Les deux marquent : oui" --print
) else (
    echo Pas de Forebet.htm : analyse des seules rencontres cotees chez Unibet.
    echo Pour ajouter les pronostics Forebet, voir le README ^(Ctrl+S sur Forebet, fichier renomme Forebet.htm ici^).
    "%PYTHON%" -m betbot --from-unibet --today --combo 4 --combo-market "Les deux marquent : oui" --print
)

:fin
echo.
pause
endlocal
