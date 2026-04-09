@echo off
chcp 65001 >nul
title VDGET - Gerenciador de Downloads

rem --- Verifica Python (cmd.exe usa PATH diferente do Git Bash / PowerShell) ---
set "PY_EXE="
set "PY_ARGS="
py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PY_EXE=py"
    set "PY_ARGS=-3"
    goto :py_ok
)
python --version >nul 2>&1
if not errorlevel 1 (
    set "PY_EXE=python"
    goto :py_ok
)
python3 --version >nul 2>&1
if not errorlevel 1 (
    set "PY_EXE=python3"
    goto :py_ok
)
echo.
echo  [ERRO] Python nao encontrado neste CMD.
echo  No Git Bash o "python" pode funcionar, mas o .bat abre o Prompt de Comando.
echo  Corrija uma destas opcoes:
echo    - Abra "Variaveis de ambiente" e adicione a pasta do Python ao PATH do usuario
echo    - Ou reinstale Python marcando "Add python.exe to PATH"
echo    - Ou use o launcher: py -3 --version  ^(deve funcionar se o Python estiver instalado^)
echo  Download: https://www.python.org/downloads/
echo.
pause & exit /b 1

:py_ok

rem --- Verifica arquivos necessarios ---
if not exist "%~dp0servidor.py" (
    echo  [ERRO] servidor.py nao encontrado na mesma pasta.
    pause & exit /b 1
)
if not exist "%~dp0index.html" (
    echo  [ERRO] index.html nao encontrado na mesma pasta.
    pause & exit /b 1
)

rem --- Instala dependencias silenciosamente se precisar ---
echo.
echo  [*] Verificando dependencias...
%PY_EXE% %PY_ARGS% -c "import websockets" >nul 2>&1
if errorlevel 1 (
    echo  [*] Instalando websockets...
    %PY_EXE% %PY_ARGS% -m pip install websockets -q
)
%PY_EXE% %PY_ARGS% -c "import yt_dlp" >nul 2>&1
if errorlevel 1 (
    echo  [*] Instalando yt-dlp...
    %PY_EXE% %PY_ARGS% -m pip install yt-dlp -q
)
echo  [OK] Dependencias prontas.

rem --- Inicia servidor em background e abre navegador ---
echo.
echo  [*] Iniciando servidor WebSocket...
start "VDGET Servidor" /min %PY_EXE% %PY_ARGS% "%~dp0servidor.py"

timeout /t 2 >nul

echo  [*] Abrindo interface no navegador...
start "" "%~dp0index.html"

echo.
echo  ============================================================
echo       VDGET esta rodando!
echo       Feche esta janela para encerrar o servidor.
echo  ============================================================
echo.

echo  Pressione qualquer tecla para ENCERRAR o servidor.
pause >nul

taskkill /fi "WindowTitle eq VDGET Servidor" /f >nul 2>&1
echo  [OK] Servidor encerrado.
timeout /t 1 >nul
