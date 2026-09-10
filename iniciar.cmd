@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto check_environment

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 goto create_with_py

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto missing_python
echo Preparando el entorno de osu! coach...
python -m venv .venv
if errorlevel 1 goto failed
goto check_environment

:create_with_py
echo Preparando el entorno de osu! coach...
py -3 -m venv .venv
if errorlevel 1 goto failed

:check_environment
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto old_environment
".venv\Scripts\python.exe" -c "from importlib.metadata import version; from pathlib import Path; specs=[line.strip().split('==', 1) for line in Path('requirements.txt').read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]; assert all(version(name)==expected for name,expected in specs)" >nul 2>&1
if not errorlevel 1 goto run
echo Instalando las dependencias de requirements.txt...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed

:run
".venv\Scripts\python.exe" app.py %*
if errorlevel 1 goto failed
exit /b 0

:missing_python
echo Necesitas Python 3.11 o posterior. Consulta la instalacion en README.md.
goto failed

:old_environment
echo El entorno .venv usa un Python anterior a 3.11.
echo Renombra esa carpeta y vuelve a iniciar con una version compatible.
goto failed

:failed
echo No se pudo iniciar. Revisa el mensaje anterior y la guia README.md.
pause
exit /b 1
