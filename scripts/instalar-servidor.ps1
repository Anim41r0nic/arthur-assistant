# Instala ARTHUR en un PC con Windows 10/11 como servidor, con Docker Desktop (docs/windows.md).
# Se ejecuta desde la carpeta del repositorio, en PowerShell (no hace falta administrador):
#
#     powershell -ExecutionPolicy Bypass -File scripts\instalar-servidor.ps1          te pregunta cada opcion
#     powershell -ExecutionPolicy Bypass -File scripts\instalar-servidor.ps1 -Si      acepta las recomendadas
#     powershell -ExecutionPolicy Bypass -File scripts\instalar-servidor.ps1 -SoloFicheros
#
# Hace lo mismo que scripts/instalar-servidor.sh (Linux): te pregunta el modelo de IA, si dejarlo siempre cargado,
# la grafica NVIDIA y la zona horaria; crea .env, config\servidor.toml y las llaves internas (secrets\, solo tu
# usuario); descarga los modelos de voz comprobando su huella SHA256; construye y arranca los contenedores; descarga
# el modelo de IA y da de alta tu primer aparato. Puedes volver a ejecutarlo: lo que ya existe, lo respeta.
# Compatible con Windows PowerShell 5.1 (el que trae Windows) y PowerShell 7.
param([switch]$Si, [switch]$SoloFicheros)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # sin la barra, Invoke-WebRequest descarga mucho mas rapido en 5.1
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path compose.yaml) -or -not (Test-Path Dockerfile)) { throw 'Ejecutalo desde la carpeta de ARTHUR (no encuentro compose.yaml)' }

function Titulo($texto) { Write-Host ''; Write-Host "== $texto ==" -ForegroundColor White }
function Aviso($texto)  { Write-Host "! $texto" -ForegroundColor Red }

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
function Preguntar([string]$Pregunta, [string]$Defecto) {
    if ($Si) { return $Defecto }
    $r = Read-Host "$Pregunta [$Defecto]"
    if ([string]::IsNullOrWhiteSpace($r)) { return $Defecto } else { return $r.Trim() }
}
# UTF-8 SIN BOM y saltos LF: Docker compose y Python leen estos ficheros; un BOM o un CRLF los rompe.
function EscribirTexto([string]$Fichero, [string]$Contenido) {
    $Contenido = $Contenido -replace "`r`n", "`n"
    if (Test-Path $Fichero) {
        $antes = [IO.File]::ReadAllText((Resolve-Path $Fichero))
        if ($antes -eq $Contenido) { Write-Host "  $Fichero (sin cambios)"; return }
        $r = 's'
        if (-not $Si) { $r = Read-Host "$Fichero ya existe y cambia. Lo sustituyo? (se guarda una copia .antes) [S/n]" }
        if ($r -and $r -notmatch '^[sSyY]') { Write-Host "  ${Fichero}: sin tocar"; return }
        Copy-Item $Fichero "$Fichero.antes" -Force
    }
    [IO.File]::WriteAllText((Join-Path (Get-Location) $Fichero), $Contenido, (New-Object Text.UTF8Encoding $false))
    Write-Host "  $Fichero OK"
}

# --- 0. Lo que hace falta ---------------------------------------------------------------------------------------
Titulo 'Comprobaciones'
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
Write-Host "RAM del PC: $ramGB GB"
if (-not $SoloFicheros) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Aviso 'Falta Docker Desktop: https://www.docker.com/products/docker-desktop/'; exit 1 }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { Aviso 'Docker Desktop no esta en marcha: abrelo, espera a que diga "Engine running" y vuelve a ejecutar esto.'; exit 1 }
    # Docker Desktop corre los contenedores en una maquina Linux (WSL2) que por defecto solo recibe la mitad de la RAM.
    $dockerGB = [math]::Round([double](docker info --format '{{.MemTotal}}') / 1GB, 1)
    Write-Host "RAM para Docker: $dockerGB GB"
    if ($dockerGB -lt 7) {
        $sugerido = [math]::Max(6, $ramGB - 2)
        Aviso "Docker tiene $dockerGB GB: el modelo de IA (~4 GB) y la voz (~1,5 GB) no caben bien."
        $op = Elegir "Darle mas memoria a Docker (fichero %USERPROFILE%\.wslconfig)?" 1 @(
            "Si, $sugerido GB", "Escribo [wsl2] memory=${sugerido}GB. Luego hay que reiniciar WSL (te digo como) y volver a ejecutar esto.",
            'No', 'Sigo igual (puede ir lento o fallar al cargar el modelo).')
        if ($op -eq 1) {
            $wslconfig = Join-Path $env:USERPROFILE '.wslconfig'
            if (Test-Path $wslconfig) { Copy-Item $wslconfig "$wslconfig.antes" -Force; Write-Host "  (copia de tu .wslconfig en $wslconfig.antes)" }
            [IO.File]::WriteAllText($wslconfig, "[wsl2]`r`nmemory=${sugerido}GB`r`n", (New-Object Text.UTF8Encoding $false))
            Write-Host ''
            Write-Host 'Hecho. Ahora: cierra Docker Desktop, ejecuta  wsl --shutdown  , vuelve a abrir Docker Desktop y' -ForegroundColor Yellow
            Write-Host 'vuelve a ejecutar este instalador.' -ForegroundColor Yellow
            exit 0
        }
    }
}

# --- 1. El modelo de IA -----------------------------------------------------------------------------------------
Titulo '1/4 - El modelo de IA (el "cerebro")'
Write-Host 'Corre en este PC con Ollama: gratis y sin que tus conversaciones salgan de aqui.'
$op = Elegir 'Que modelo quieres?' 1 @(
    'qwen3-vl:4b-instruct', 'Texto, herramientas y fotos (dice que hay en una imagen). ~3,3 GB de descarga.',
    'qwen3:4b-instruct', 'Solo texto y herramientas, sin fotos. Algo mas ligero.',
    'Otro de Ollama', 'Escribes su nombre (https://ollama.com/library). Comprueba antes cuanta RAM necesita.')
switch ($op) {
    1 { $modelo = 'qwen3-vl:4b-instruct'; $vision = 'true' }
    2 { $modelo = 'qwen3:4b-instruct'; $vision = 'false' }
    3 {
        $modelo = Preguntar 'Nombre del modelo (p. ej. qwen3:8b)' 'qwen3:4b-instruct'
        $v = Elegir 'Ese modelo ve fotos?' 2 @('Si', 'La web dejara adjuntar fotos.', 'No / no lo se', 'Si no las ve, la web no deja adjuntarlas.')
        if ($v -eq 1) { $vision = 'true' } else { $vision = 'false' }
    }
}

# --- 2. Siempre cargado ------------------------------------------------------------------------------------------
Titulo '2/4 - Dejar el modelo siempre cargado en memoria?'
$rec = 1; if ($ramGB -ge 14) { $rec = 2 }
$op = Elegir 'Ocupa ~4 GB de RAM mientras esta cargado.' $rec @(
    'Que se descargue si no lo usas (30 min)', 'Deja la RAM libre. La primera pregunta tras un rato sin usarlo tarda mas (solo con procesador, ~2 min).',
    'Siempre cargado', 'Responde rapido siempre, pero esos ~4 GB quedan ocupados. Recomendado con 16 GB o mas.')
if ($op -eq 2) { $keepAlive = '-1m' } else { $keepAlive = '30m' }

# --- 3. La grafica -----------------------------------------------------------------------------------------------
Titulo '3/4 - La grafica'
$ficheros = 'compose.yaml'
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $nombreGpu = (nvidia-smi --query-gpu=name --format=csv,noheader | Select-Object -First 1)
    Write-Host "He encontrado una NVIDIA: $nombreGpu"
    $op = Elegir 'Que la IA la use (CUDA)?' 1 @(
        'Si, usar la NVIDIA', 'Mucho mas rapido que el procesador si el modelo cabe en su memoria.',
        'No, solo el procesador', 'Mas lento.')
    if ($op -eq 1) { $ficheros += ';compose.nvidia.yaml' }
} else {
    Write-Host 'No he encontrado una NVIDIA: la IA usara el procesador.'
    Write-Host 'Las graficas integradas (Intel/AMD) no se pueden usar desde Docker Desktop en Windows.' -ForegroundColor DarkGray
}

# --- 4. Hora y avisos -------------------------------------------------------------------------------------------
Titulo '4/4 - Zona horaria y avisos al movil'
$zona = 'Europe/Madrid'
try {
    $iana = $null
    if ([TimeZoneInfo]::TryConvertWindowsIdToIanaId([TimeZoneInfo]::Local.Id, [ref]$iana)) { $zona = $iana }
} catch {     # Windows PowerShell 5.1 no sabe convertir: se pregunta
    Write-Verbose "Sin conversion automatica de zona horaria: $($_.Exception.Message)"
}
$zona = Preguntar 'Zona horaria (formato Europa/Ciudad, p. ej. Europe/Madrid)' $zona
$tsNombre = ''
if (Get-Command tailscale -ErrorAction SilentlyContinue) {
    try { $tsNombre = ((tailscale status --json | Out-String | ConvertFrom-Json).Self.DNSName).TrimEnd('.') }
    catch { Write-Verbose "Tailscale no ha dado su nombre: $($_.Exception.Message)" }   # sin nombre: se usa un correo
}
if ($tsNombre) { $sujetoDef = "https://$tsNombre" } else { $sujetoDef = 'mailto:tu@correo.com' }
Write-Host 'Apple y Google piden saber quien manda los avisos: la direccion de tu ARTHUR (https://...) o un correo (mailto:...).'
$sujeto = Preguntar 'Quien manda los avisos' $sujetoDef
if ($sujeto -notmatch '^(https://|mailto:)') { Aviso 'Debe empezar por https:// o mailto:'; exit 1 }

# --- Escribir la configuracion -----------------------------------------------------------------------------------
Titulo 'Configuracion'
EscribirTexto '.env' @"
# Creado por scripts\instalar-servidor.ps1 (puedes editarlo; explicacion en .env.example)
# En Windows los ficheros de compose se separan con ';'
COMPOSE_FILE=$ficheros
TZ=$zona
ARTHUR_PUSH_SUBJECT=$sujeto
"@
EscribirTexto 'config\servidor.toml' @"
# Creado por scripts\instalar-servidor.ps1. Para cambiar algo: editalo y  docker compose restart core
# Todas las opciones: config/config.example.toml

[brain]
ollama_url = "http://ollama:11434"
model = "$modelo"
vision = $vision
timeout = 300
# "30m": se descarga si no le hablas en 30 min - "-1m": siempre cargado
keep_alive = "$keepAlive"
"@

# Las llaves internas (core <-> voz, core <-> push). Nunca salen de este PC. La carpeta, solo para tu usuario.
New-Item -ItemType Directory -Force secrets | Out-Null
foreach ($llave in 'voz_token', 'push_token') {
    $ruta = "secrets\$llave"
    if (-not (Test-Path $ruta) -or (Get-Item $ruta).Length -eq 0) {
        $bytes = New-Object byte[] 32
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
        [IO.File]::WriteAllText((Join-Path (Get-Location) $ruta), "$token`n", (New-Object Text.UTF8Encoding $false))
        Write-Host "  $ruta OK (nueva)"
    }
}
icacls secrets /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" *> $null
if ($SoloFicheros) { Write-Host ''; Write-Host 'Listo (solo ficheros). Para seguir, vuelve a ejecutarlo sin -SoloFicheros.'; exit 0 }

# --- Modelos de voz (comprobados por SHA256) ---------------------------------------------------------------------
Titulo 'Modelos de voz'
function Descargar([string]$Url, [string]$Destino, [string]$Huella) {
    if ((Test-Path $Destino) -and (-not $Huella -or (Get-FileHash $Destino -Algorithm SHA256).Hash.ToLower() -eq $Huella)) {
        Write-Host "  $Destino OK (ya estaba)"; return
    }
    New-Item -ItemType Directory -Force (Split-Path $Destino) | Out-Null
    Write-Host "  Descargando $Destino..."
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile "$Destino.parcial"
    if ($Huella -and (Get-FileHash "$Destino.parcial" -Algorithm SHA256).Hash.ToLower() -ne $Huella) {
        Remove-Item "$Destino.parcial"; throw "La huella de $Destino no coincide: no lo uso"
    }
    Move-Item "$Destino.parcial" $Destino -Force
    Write-Host "  $Destino OK"
}
# whisper.cpp (MIT): el oido. Piper es-ES davefx (CC0): la voz. Las mismas huellas que comprueba arthur-voz.
Descargar 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small-q5_1.bin' 'modelos\whisper\ggml-small-q5_1.bin' 'ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb'
$voz = 'https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/davefx/medium'
Descargar "$voz/es_ES-davefx-medium.onnx" 'modelos\voices\es_ES-davefx-medium.onnx' '6658b03b1a6c316ee4c265a9896abc1393353c2d9e1bca7d66c2c442e222a917'
Descargar "$voz/es_ES-davefx-medium.onnx.json" 'modelos\voices\es_ES-davefx-medium.onnx.json' ''

# --- Construir y arrancar ----------------------------------------------------------------------------------------
Titulo 'Construir y arrancar (la primera vez tarda: compila whisper.cpp para ESTE procesador)'
docker compose build;  if ($LASTEXITCODE -ne 0) { throw 'Fallo al construir las imagenes' }
docker compose up -d;  if ($LASTEXITCODE -ne 0) { throw 'Fallo al arrancar los contenedores' }
Write-Host "Descargando el modelo de IA $modelo (varios GB)..."
docker compose exec -T ollama ollama pull $modelo; if ($LASTEXITCODE -ne 0) { throw 'Fallo al descargar el modelo' }
docker compose restart core | Out-Null

# --- Primer aparato ------------------------------------------------------------------------------------------------
Titulo 'Tu primer aparato'
Write-Host 'Cada aparato (movil, portatil, navegador...) tiene su propia llave, que puedes retirar cuando quieras.'
$aparato = Preguntar 'Nombre del primer aparato' 'movil'
docker compose exec -T core python -m arthur --dispositivo-nuevo $aparato

Titulo 'Hecho'
if ($tsNombre) {
    Write-Host 'Para usar la web desde tus aparatos (con HTTPS, que el micro y los avisos necesitan):'
    Write-Host '    tailscale serve --bg 8770'
    Write-Host "y abre en cada aparato:  https://$tsNombre"
} else {
    Write-Host 'La web: http://127.0.0.1:8770 (en este PC). Para tus otros aparatos, instala Tailscale (docs/windows.md).'
}
Write-Host ''
Write-Host 'Para que ARTHUR siga en marcha (docs/windows.md):'
Write-Host '  - Docker Desktop: Settings > General > "Start Docker Desktop when you sign in".'
Write-Host '  - El PC no debe suspenderse (Configuracion > Sistema > Inicio/apagado y suspension > Nunca).'
Write-Host '  - Mas aparatos:  docker compose exec core python -m arthur --dispositivo-nuevo NOMBRE'
