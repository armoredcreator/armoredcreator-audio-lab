@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ARMORED_ROOT=%~dp0"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
rem Vision V1 + Caption ativos.
set "ARMORED_CAPTION_ENABLED=1"
set "ARMORED_CAPTION_MODEL=gemini-3.1-flash-lite"
set "ARMORED_CAPTION_API_TIMEOUT=90"
set "ARMORED_CAPTION_ALLOW_DETERMINISTIC_FALLBACK=0"

set "ARMORED_PYTHON="
where py >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)"') do set "ARMORED_PYTHON=%%P"
)
if not defined ARMORED_PYTHON (
    where python >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%P in ('where python') do if not defined ARMORED_PYTHON set "ARMORED_PYTHON=%%P"
    )
)
if not defined ARMORED_PYTHON (
    echo ERRO: Python nao encontrado.
    exit /b 1
)

echo ================================================================
echo ARMORED CREATOR - START ALL
echo SYNC -^> SQLITE -^> COORDINATOR -^> VISION -^> STUDIO -^> HUB -^> TELEGRAM
echo ================================================================
echo Python: %ARMORED_PYTHON%
echo Root:   %ARMORED_ROOT%
echo.
echo Coordinator e a unica raiz de composicao da pipeline.
echo CATCH-UP -^> LIVE -^> processamento sequencial.
echo Para shutdown controlado: CTRL+C
echo.

"%ARMORED_PYTHON%" "%ARMORED_ROOT%run_coordinator.py"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ArmoredCreator encerrou. Codigo: %EXIT_CODE%
exit /b %EXIT_CODE%
