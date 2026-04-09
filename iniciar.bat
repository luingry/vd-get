@echo off
chcp 65001 >nul
title VDGET - Gerenciador de Downloads

rem --- Verifica Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERRO] Python nao encontrado.
    echo  Instale em: https://www.python.org/downloads/
    echo  Marque "Add Python to PATH" durante a instalacao.
    echo.
    pause & exit /b 1
)

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
python -c "import websockets" >nul 2>&1
if errorlevel 1 (
    echo  [*] Instalando websockets...
    python -m pip install websockets -q
)
python -c "import yt_dlp" >nul 2>&1
if errorlevel 1 (
    echo  [*] Instalando yt-dlp...
    python -m pip install yt-dlp -q
)
echo  [OK] Dependencias prontas.

rem --- Inicia servidor em background e abre navegador ---
echo.
echo  [*] Iniciando servidor WebSocket...
start "VDGET Servidor" /min python "%~dp0servidor.py"

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
