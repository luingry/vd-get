@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title VDGET — registrar protocolo vdget://

if not exist "%~dp0iniciar.bat" (
  echo [ERRO] iniciar.bat nao encontrado nesta pasta.
  pause & exit /b 1
)

set "DIR=%~dp0"
set "DIRX=!DIR:\=\\!"

set "REG=%TEMP%\vdget_protocol_install.reg"
(
echo Windows Registry Editor Version 5.00
echo.
echo [HKEY_CURRENT_USER\Software\Classes\vdget]
echo @="URL:VDGET"
echo "URL Protocol"=""
echo.
echo [HKEY_CURRENT_USER\Software\Classes\vdget\shell]
echo.
echo [HKEY_CURRENT_USER\Software\Classes\vdget\shell\open]
echo.
echo [HKEY_CURRENT_USER\Software\Classes\vdget\shell\open\command]
echo @="cmd.exe /c \"\"!DIRX!iniciar.bat\"\""
) > "%REG%"

reg import "%REG%" >nul 2>&1
if errorlevel 1 (
  echo [ERRO] Nao foi possivel importar o registo. Verifique permissoes ^(HKCU nao exige admin^).
  del "%REG%" 2>nul
  pause & exit /b 1
)

del "%REG%" 2>nul

echo.
echo  [OK] Protocolo vdget:// registado para:
echo      %DIR%iniciar.bat
echo.
echo  No navegador use o link "Ligar VDGET" na pagina ou digite na barra: vdget://iniciar
echo.
pause
