@echo off
rem ARTHUR en Windows: escribir «arthur» en cualquier terminal (cmd o PowerShell) abre la web de tu servidor.
rem No instala nada ni guarda ninguna llave: solo la DIRECCIÓN de la web, en %APPDATA%\arthur\web.txt.
rem   La primera vez:  arthur configurar https://TU-SERVIDOR.ts.net
rem   Después:         arthur
rem Instalar: copiar este fichero a %LOCALAPPDATA%\Microsoft\WindowsApps (esa carpeta ya está en el PATH).
setlocal
set "FILE=%APPDATA%\arthur\web.txt"

if /i "%~1"=="configurar" goto configurar

if not exist "%FILE%" (
  echo Aun no se donde esta tu ARTHUR. La primera vez: arthur configurar https://TU-SERVIDOR.ts.net
  exit /b 1
)
set /p URL=<"%FILE%"
rem Solo se abre una dirección https (o http dentro de casa); nunca otra cosa que hubiera en el fichero.
echo %URL%| findstr /r /b /c:"https://[A-Za-z0-9.-]*[A-Za-z0-9]/*$" /c:"http://[0-9.]*:[0-9]*/*$" >nul || (
  echo La direccion guardada no es valida: %URL%
  exit /b 1
)
start "" "%URL%"
exit /b 0

:configurar
set "NEW=%~2"
echo %NEW%| findstr /r /b /c:"https://[A-Za-z0-9.-]*[A-Za-z0-9]/*$" /c:"http://[0-9.]*:[0-9]*/*$" >nul || (
  echo Escribe la direccion completa, por ejemplo: arthur configurar https://arthur-server.tu-red.ts.net
  exit /b 1
)
if not exist "%APPDATA%\arthur" mkdir "%APPDATA%\arthur"
> "%FILE%" echo %NEW%
echo Listo. Desde ahora, arthur abre %NEW%
start "" "%NEW%"
exit /b 0
