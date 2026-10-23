@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist logs mkdir logs
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%I"
set "ICLOUD_MIGRATOR_LOG=%~dp0logs\icloud_%STAMP%.log"

if not exist config.json (
    echo Primera ejecucion: vamos a configurar la cuenta y la carpeta de destino.
    python -X utf8 migrate_icloud.py --setup
)

:MENU
echo.
echo ========================================
echo   CloudPull - Descarga de albumes de iCloud
echo ========================================
echo 1. Iniciar sesion en iCloud (abre una ventana)
echo 2. Actualizar inventario de albumes
echo 3. Ver albumes pendientes
echo 4. Descargar TODOS los pendientes (los fallos van a logs\fallos_albumes.log)
echo 5. Descargar un album concreto
echo 6. Cambiar cuenta / carpeta de destino
echo 7. Salir
choice /c 1234567 /n /m "Elige una opcion: "
if errorlevel 7 goto END
if errorlevel 6 ( python -X utf8 migrate_icloud.py --setup & goto MENU )
if errorlevel 5 ( python -X utf8 migrate_icloud.py --choose-album & pause & goto MENU )
if errorlevel 4 ( python -X utf8 migrate_icloud.py --all & pause & goto MENU )
if errorlevel 3 ( python -X utf8 migrate_icloud.py --pending & pause & goto MENU )
if errorlevel 2 ( python -X utf8 migrate_icloud.py --inventory & pause & goto MENU )
if errorlevel 1 ( python -X utf8 migrate_icloud.py --login & pause & goto MENU )
goto MENU

:END
echo Log de la sesion: %ICLOUD_MIGRATOR_LOG%
