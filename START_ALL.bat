@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ARMORED_ROOT=%~dp0"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"

rem ================================================================
rem ARMORED CREATOR - START ALL
rem TELEGRAM BOT API LOCAL -> COORDINATOR
rem ================================================================

rem Vision V1 + ArmoredIA/Caption ativos.
set "ARMORED_IA_ENABLED=1"
set "ARMORED_IA_CAPTION_ENABLED=1"
set "ARMORED_IA_MODEL=gemini-3.1-flash-lite"
set "ARMORED_IA_API_TIMEOUT=90"
set "ARMORED_IA_MAX_CANDIDATES=10"
set "ARMORED_IA_MAX_ATTEMPTS=5"
set "ARMORED_IA_RETRY_DELAY=2"

rem Caminho do Telegram Bot API nesta maquina.
rem Para outra maquina, altere somente esta linha.
set "BOT_API_EXE=C:\Users\Administrador\Downloads\TelegramBotAPI\telegram-bot-api\build\Release\telegram-bot-api.exe"
set "BOT_API_PORT=8081"
set "ARMORED_TELEGRAM_BOT_API_URL=http://127.0.0.1:%BOT_API_PORT%/bot"
set "ARMORED_TELEGRAM_BOT_API_FILE_URL=http://127.0.0.1:%BOT_API_PORT%/file/bot"
set "BOT_API_STARTED_BY_SCRIPT=0"
set "BOT_API_PID="
set "BOT_API_DIR="

for %%I in ("%BOT_API_EXE%") do set "BOT_API_DIR=%%~dpI"

rem ================================================================
rem 1. Carrega API ID/HASH do credentials\project.env
rem    Os valores nao sao exibidos.
rem ================================================================

if not exist "%ARMORED_ROOT%credentials\project.env" (
    echo ERRO: credentials\project.env nao encontrado.
    exit /b 1
)

for /f "delims=" %%A in ('powershell -NoProfile -Command "$v=Get-Content -LiteralPath '%ARMORED_ROOT%credentials\\project.env' | ForEach-Object { $_ -replace '^\\uFEFF','' } | Where-Object { $_ -match '^TELEGRAM_API_ID=' } | Select-Object -First 1; if($v){$v -replace '^TELEGRAM_API_ID=',''}"') do set "TELEGRAM_API_ID=%%A"
for /f "delims=" %%A in ('powershell -NoProfile -Command "$v=Get-Content -LiteralPath '%ARMORED_ROOT%credentials\\project.env' | ForEach-Object { $_ -replace '^\\uFEFF','' } | Where-Object { $_ -match '^TELEGRAM_API_HASH=' } | Select-Object -First 1; if($v){$v -replace '^TELEGRAM_API_HASH=',''}"') do set "TELEGRAM_API_HASH=%%A"

if not defined TELEGRAM_API_ID (
    echo ERRO: TELEGRAM_API_ID nao encontrado em credentials\project.env
    exit /b 1
)

if not defined TELEGRAM_API_HASH (
    echo ERRO: TELEGRAM_API_HASH nao encontrado em credentials\project.env
    exit /b 1
)

echo ================================================================
echo ARMORED CREATOR - START ALL
echo SYNC -^> SQLITE -^> COORDINATOR -^> VISION -^> STUDIO -^> HUB -^> TELEGRAM
echo ================================================================
echo Root: %ARMORED_ROOT%
echo.

rem ================================================================
rem 2. Verifica se o Telegram Bot API ja esta ativo.
rem    Se ja estiver, nao inicia outra instancia.
rem ================================================================

echo [1/3] Verificando Telegram Bot API local...

powershell -NoProfile -Command "$r=Test-NetConnection 127.0.0.1 -Port %BOT_API_PORT% -WarningAction SilentlyContinue; if($r.TcpTestSucceeded){exit 0}else{exit 1}"

if not errorlevel 1 (
    echo       Bot API ja esta ativo em 127.0.0.1:%BOT_API_PORT%
    goto BOT_API_READY
)

echo       Bot API nao esta ativo. Iniciando...

if not exist "%BOT_API_EXE%" (
    echo.
    echo ERRO: Telegram Bot API nao encontrado:
    echo %BOT_API_EXE%
    exit /b 1
)

rem O servidor oficial aceita API ID/HASH por ambiente ou argumentos.
rem Passamos explicitamente os valores para tornar o processo independente do
rem ambiente herdado e garantir que a instancia local esteja corretamente configurada.
powershell -NoProfile -Command "$p=Start-Process -FilePath '%BOT_API_EXE%' -ArgumentList '--api-id=%TELEGRAM_API_ID%','--api-hash=%TELEGRAM_API_HASH%','--local','--http-port=%BOT_API_PORT%' -WorkingDirectory '%BOT_API_DIR%' -WindowStyle Minimized -PassThru; Write-Output $p.Id" > "%TEMP%\armored_bot_api_pid.txt"

if errorlevel 1 (
    echo ERRO: nao foi possivel iniciar o Telegram Bot API.
    exit /b 1
)

set /p BOT_API_PID=<"%TEMP%\armored_bot_api_pid.txt"
del "%TEMP%\armored_bot_api_pid.txt" >nul 2>&1

set "BOT_API_STARTED_BY_SCRIPT=1"

echo       Bot API iniciado. PID: %BOT_API_PID%
echo       Aguardando porta %BOT_API_PORT%...

rem ================================================================
rem 3. Aguarda o servidor responder na porta 8081.
rem ================================================================

powershell -NoProfile -Command "$deadline=(Get-Date).AddSeconds(30); do { $r=Test-NetConnection 127.0.0.1 -Port %BOT_API_PORT% -WarningAction SilentlyContinue; if($r.TcpTestSucceeded){exit 0}; Start-Sleep -Seconds 1 } while((Get-Date) -lt $deadline); exit 1"

if errorlevel 1 (
    echo.
    echo ERRO: Telegram Bot API nao ficou disponivel em 30 segundos.

    if defined BOT_API_PID (
        taskkill /PID %BOT_API_PID% /T /F >nul 2>&1
    )

    exit /b 1
)

:BOT_API_READY

echo       Bot API local: OK
echo.

rem ================================================================
rem 4. Localiza Python.
rem ================================================================

echo [2/3] Localizando Python...

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

    if "%BOT_API_STARTED_BY_SCRIPT%"=="1" if defined BOT_API_PID (
        taskkill /PID %BOT_API_PID% /T /F >nul 2>&1
    )

    exit /b 1
)

echo       Python: %ARMORED_PYTHON%
echo.

rem ================================================================
rem 5. Inicia Coordinator.
rem ================================================================

echo [3/3] Iniciando ArmoredCreator...
echo.
echo Coordinator e a unica raiz de composicao da pipeline.
echo CATCH-UP -^> LIVE -^> processamento sequencial.
echo Para shutdown controlado: CTRL+C
echo.

"%ARMORED_PYTHON%" "%ARMORED_ROOT%run_coordinator.py"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ArmoredCreator encerrou. Codigo: %EXIT_CODE%

rem ================================================================
rem 6. Encerra somente o Bot API iniciado por ESTE START_ALL.
rem ================================================================

if "%BOT_API_STARTED_BY_SCRIPT%"=="1" if defined BOT_API_PID (
    echo.
    echo Encerrando Telegram Bot API iniciado pelo START ALL...
    taskkill /PID %BOT_API_PID% /T /F >nul 2>&1
)

exit /b %EXIT_CODE%
