# Instala este PC con Windows 10/11 como APARATO de tu ARTHUR (docs/windows.md, fase A).
# El cerebro sigue en tu servidor; este PC pone el microfono, el altavoz, el Orb y sus "manos"
# (abrir apps del menu Inicio, volumen y musica).
# Se ejecuta desde la carpeta del repositorio, en PowerShell (NO hace falta administrador):
#
#     powershell -ExecutionPolicy Bypass -File scripts\windows\instalar-aparato.ps1          te pregunta cada opcion
#     powershell -ExecutionPolicy Bypass -File scripts\windows\instalar-aparato.ps1 -Si      acepta las recomendadas
#     powershell -ExecutionPolicy Bypass -File scripts\windows\instalar-aparato.ps1 -Quitar  quita el arranque y el atajo
#
# Que hace: comprueba Python (3.14 o mas nuevo, de python.org); empareja este PC con tu servidor (la llave la pegas
# tu: no se ve al escribir y se guarda en %APPDATA%\arthur\cliente.toml); crea el arranque al iniciar sesion (una
# tarea programada de TU usuario, sin permisos de administrador) y el atajo Ctrl+Alt+A para hablarle.
# No descarga ni instala nada de internet. Puedes volver a ejecutarlo: lo que ya existe, lo respeta.
# Compatible con Windows PowerShell 5.1 (el que trae Windows) y PowerShell 7. Solo ASCII a proposito.
param([switch]$Si, [switch]$Quitar)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $repo
if (-not (Test-Path 'pyproject.toml') -or -not (Test-Path 'arthur\__main__.py')) {
    throw 'Ejecutalo desde la carpeta de ARTHUR (no encuentro arthur\__main__.py)'
}

$tarea = 'ARTHUR (aparato)'
$menuInicio = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$atajo = Join-Path $menuInicio 'ARTHUR - hablar.lnk'
$inicioSesion = Join-Path $menuInicio 'Startup\ARTHUR (aparato).lnk'
$argsServicio = '-m arthur --servicio-cliente'
$usuario = "$env:USERDOMAIN\$env:USERNAME"

function Titulo([string]$Texto) { Write-Host ''; Write-Host "== $Texto ==" -ForegroundColor White }
function Aviso([string]$Texto) { Write-Host "! $Texto" -ForegroundColor Red }

# Elegir 'pregunta' recomendada @(nombre, explicacion, nombre, explicacion...) -> numero elegido (1, 2...)
function Elegir([string]$Pregunta, [int]$Recomendada, [string[]]$Opciones) {
    Write-Host ''; Write-Host $Pregunta
    $n = $Opciones.Count / 2
    for ($i = 0; $i -lt $n; $i++) {
        $num = $i + 1
        if ($num -eq $Recomendada) {
            Write-Host ("  {0}) {1} (recomendado)" -f $num, $Opciones[2 * $i]) -ForegroundColor Green
        } else {
            Write-Host ("  {0}) {1}" -f $num, $Opciones[2 * $i])
        }
        Write-Host ("     " + $Opciones[2 * $i + 1]) -ForegroundColor DarkGray
    }
    if ($Si) { Write-Host "  -> $Recomendada"; return $Recomendada }
    while ($true) {
        $r = Read-Host "Elige [$Recomendada]"
        if ([string]::IsNullOrWhiteSpace($r)) { return $Recomendada }
        $v = 0
        if ([int]::TryParse($r, [ref]$v) -and $v -ge 1 -and $v -le $n) { return $v }
        Write-Host "  Escribe un numero del 1 al $n."
    }
}

function QuitarArranque {
    if (Get-ScheduledTask -TaskName $tarea -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $tarea -Confirm:$false
        Write-Host "  Tarea '$tarea' quitada"
    }
    foreach ($f in $inicioSesion, $atajo) {
        if (Test-Path $f) { Remove-Item $f; Write-Host "  $f quitado" }
    }
}

# WScript.Shell viene con Windows: crea un acceso directo .lnk normal (como el de cualquier app).
function NuevoAcceso([string]$Fichero, [string]$Programa, [string]$Argumentos, [string]$Tecla) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Fichero)
    $lnk.TargetPath = $Programa
    $lnk.Arguments = $Argumentos
    $lnk.WorkingDirectory = $repo
    if ($Tecla) { $lnk.Hotkey = $Tecla }
    $lnk.Save()
}

if ($Quitar) {
    Titulo 'Quitar el arranque y el atajo de ARTHUR'
    QuitarArranque
    Write-Host 'Hecho. Tu emparejamiento (%APPDATA%\arthur\cliente.toml) sigue ahi; borralo si ya no quieres este aparato.'
    exit 0
}

# --- 0. Python ------------------------------------------------------------------------------------------------
Titulo 'Comprobaciones'
$python = $null
$prueba = 'import sys; print(sys.version_info >= (3, 14)); print(sys.executable)'
foreach ($candidato in @(@('py', '-3'), @('python'))) {
    if (-not (Get-Command $candidato[0] -ErrorAction SilentlyContinue)) { continue }
    try {
        $argumentos = @($candidato | Select-Object -Skip 1) + @('-c', $prueba)
        $salida = @(& $candidato[0] @argumentos 2>$null)
        if ($LASTEXITCODE -eq 0 -and $salida.Count -ge 2 -and $salida[0] -eq 'True') { $python = $salida[1]; break }
    } catch {
        Write-Verbose "No sirve $($candidato[0]): $($_.Exception.Message)"
    }
}
if (-not $python) {
    Aviso 'Falta Python 3.14 o mas nuevo.'
    Write-Host '  Descargalo de https://www.python.org/downloads/windows/ (el instalador de 64 bits) y, al instalarlo,'
    Write-Host '  marca "Add python.exe to PATH". Despues vuelve a ejecutar este instalador.'
    exit 1
}
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { Aviso "No encuentro pythonw.exe junto a $python"; exit 1 }
Write-Host "Python: $python"

# --- 1. Emparejar ---------------------------------------------------------------------------------------------
Titulo '1/3 - Conectar con tu servidor'
$cliente = Join-Path $env:APPDATA 'arthur\cliente.toml'
$emparejar = $true
if (Test-Path $cliente) {
    $op = Elegir 'Este PC ya esta emparejado. Que hago?' 1 @(
        'Dejarlo como esta', 'Sigue usando el servidor y la llave que ya tiene.',
        'Emparejar de nuevo', 'Con otra direccion o una llave nueva (en el servidor: --dispositivo-nuevo NOMBRE).')
    $emparejar = ($op -eq 2)
}
if ($emparejar) {
    if ($Si) { Aviso 'Para emparejar hace falta pegar la llave: ejecutalo sin -Si.'; exit 1 }
    Write-Host 'En el servidor, da de alta este PC y copia su llave:'
    Write-Host '    docker compose exec core python -m arthur --dispositivo-nuevo pc-windows' -ForegroundColor DarkGray
    $url = ''
    while ($url -notmatch '^https?://[A-Za-z0-9.-]+(:[0-9]+)?/?$') {
        $url = (Read-Host 'Direccion del servidor (p. ej. https://arthur-server.tu-red.ts.net)').Trim()
    }
    & $python -m arthur --emparejar $url
    if ($LASTEXITCODE -ne 0) { Aviso 'No se ha podido emparejar (mira la direccion y la llave).'; exit 1 }
}

# --- 2. Arrancar al iniciar sesion ------------------------------------------------------------------------------
Titulo '2/3 - Arrancar ARTHUR al iniciar sesion'
$modo = Elegir 'Como lo arranco?' 1 @(
    'Tarea programada al iniciar sesion', 'Arranca sola al entrar en tu usuario y se reinicia si se cae. Sin administrador.',
    'Carpeta Inicio', 'Un acceso directo en Inicio de Windows: mas simple, pero no se reinicia si se cae.',
    'No arrancarlo solo', 'Lo arrancas tu cuando quieras con:  pythonw -m arthur --servicio-cliente')
QuitarArranque
if ($modo -eq 1) {
    try {
        $accion = New-ScheduledTaskAction -Execute $pythonw -Argument $argsServicio -WorkingDirectory $repo
        $cuando = New-ScheduledTaskTrigger -AtLogOn -User $usuario
        # Tu usuario, en tu sesion (asi puede usar el micro y el altavoz) y SIN administrador (Limited).
        $quien = New-ScheduledTaskPrincipal -UserId $usuario -LogonType Interactive -RunLevel Limited
        $ajustes = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $tarea -Action $accion -Trigger $cuando -Principal $quien -Settings $ajustes | Out-Null
        Write-Host "  Tarea '$tarea' OK"
    } catch {
        Aviso "Windows no me deja crear la tarea ($($_.Exception.Message)). Uso la carpeta Inicio."
        $modo = 2
    }
}
if ($modo -eq 2) {
    New-Item -ItemType Directory -Force (Split-Path $inicioSesion) | Out-Null
    NuevoAcceso $inicioSesion $pythonw $argsServicio ''
    Write-Host "  $inicioSesion OK"
}

# --- 3. El atajo para hablarle -----------------------------------------------------------------------------------
Titulo '3/3 - El atajo para hablarle'
$op = Elegir 'Crear el atajo de teclado?' 1 @(
    'Si, Ctrl+Alt+A', 'Pulsalo para que te escuche y otra vez cuando termines (como Super+Shift+A en Linux).',
    'No', 'Sin atajo no hay forma de hablarle por voz desde este PC (de momento no hay "Hey Arthur" en Windows).')
if ($op -eq 1) {
    $hablar = Join-Path $repo 'scripts\windows\arthur-hablar.pyw'
    NuevoAcceso $atajo $pythonw ('"' + $hablar + '"') 'CTRL+ALT+A'
    Write-Host "  $atajo OK (atajo Ctrl+Alt+A)"
}

if ($modo -eq 1) {
    Start-ScheduledTask -TaskName $tarea
} elseif ($modo -eq 2) {
    Start-Process -FilePath $pythonw -ArgumentList $argsServicio -WorkingDirectory $repo
}

Titulo 'Hecho'
Write-Host 'Para probarlo:'
Write-Host '  - Pulsa Ctrl+Alt+A, di algo y vuelve a pulsarlo.'
Write-Host '  - El Orb (la pantalla de ARTHUR):  python -m arthur --abrir-orb'
Write-Host '  - El registro:  %LOCALAPPDATA%\arthur\arthur.log'
Write-Host '  - Quitarlo todo:  powershell -ExecutionPolicy Bypass -File scripts\windows\instalar-aparato.ps1 -Quitar'
Write-Host 'Si no te oye: Configuracion > Privacidad > Microfono > "Permitir que las apps de escritorio accedan al microfono".'
