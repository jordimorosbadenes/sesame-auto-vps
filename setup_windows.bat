@echo off
echo.
echo === Instalando dependencias para sesame_auto ===
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python no encontrado. Instálalo desde https://python.org
    pause
    exit /b 1
)

echo Instalando playwright y python-dotenv...
pip install playwright python-dotenv

echo.
echo Instalando Chromium...
python -m playwright install chromium

echo.
echo === Listo. Ahora ejecuta: ===
echo.
echo   python test_fichar.py
echo.
pause
