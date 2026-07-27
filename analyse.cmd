@echo off
rem Lanceur double-cliquable : analyse les prochaines rencontres cotees chez Unibet
rem et affiche le rapport dans la console.
rem
rem   analyse.cmd                       analyse par defaut (10 matchs, ticket de 4 BTTS)
rem   analyse.cmd --matches 20 --print  tes propres options, passees telles quelles

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

if "%~1"=="" (
    "%PYTHON%" -m betanalyst --from-unibet --matches 10 --combo 4 --combo-market "Les deux marquent : oui" --print
) else (
    "%PYTHON%" -m betanalyst %*
)

echo.
pause
endlocal
