@echo off
rem Construit dist\Bet.Bot.exe : un seul fichier, sans Python installe sur la machine cible.
rem
rem Le navigateur Chromium n'est pas embarque (300 Mo) : il reste installe une fois pour
rem toutes par "playwright install chromium", ou par Bet.Bot.exe --no-flashscore sinon.

setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

"%PYTHON%" -m pip install --upgrade pyinstaller
if errorlevel 1 goto :fin

"%PYTHON%" -m PyInstaller --noconfirm --onefile --console ^
    --name "Bet.Bot" ^
    --collect-all playwright ^
    --hidden-import bs4 ^
    betbot\__main__.py

echo.
echo Executable : %~dp0dist\Bet.Bot.exe

:fin
echo.
pause
endlocal
