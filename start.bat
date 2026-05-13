@echo off
echo.
echo  ============================================================
echo   AI QUANT RESEARCH TERMINAL - ZAGON
echo  ============================================================
echo.

cd /d "%~dp0"

echo  Preverja Python...
python --version
if errorlevel 1 (
    echo.
    echo  NAPAKA: Python ni najden!
    echo  Namesti Python z https://www.python.org/downloads/
    echo  POMEMBNO: Pri namestitvi obkljukaj "Add Python to PATH"
    echo.
    pause
    exit /b
)

echo.
echo  [1/2] Nameščam pakete (flask, yfinance, pandas)...
pip install flask yfinance pandas numpy
if errorlevel 1 (
    echo.
    echo  NAPAKA pri namestitvi paketov!
    pause
    exit /b
)

echo.
echo  [2/2] Ustavljam stare Python procese na portu 5001...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5001"') do (
    echo  Ubijam proces %%a...
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo  [3/3] Testiram Flask...
python -c "from flask import Flask; a=Flask(__name__); print('Flask: OK')"

echo  [4/4] Testiram templates mapo...
python -c "import os; print('templates:', os.path.exists('templates')); print('terminal.html:', os.path.exists('templates/terminal.html'))"

echo  [5/5] Testiram uvoz app.py...
python -c "import sys; sys.path.insert(0,'.'); import app; print('app.py uvoz: OK')"

echo  [6/6] Zaganjam server...
echo.
echo  ============================================================
echo   Odpri brskalnik na:  http://localhost:5001
echo   Za zaustavitev:      pritisni Ctrl+C
echo  ============================================================
echo.

python -u app.py

echo.
echo  ============================================================
echo  SERVER SE JE USTAVIL
echo  ============================================================
pause
