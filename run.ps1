# Build and run ArchonEngine ControlPanel in Podman on Windows (the Windows branch's run.sh).
#
#   .\run.ps1                         build the image and (re)start the container
#   .\run.ps1 -DataDir D:\Kawari      another Kawari config folder (default: %USERPROFILE%\.kawari)
#
# Podman on Windows runs containers in a Linux VM ("podman machine", on WSL2). The ControlPanel runs in a container
# there and drives the server, builds and kawari-questdump through that machine's Podman API socket, as it does on
# Linux. Two things differ from run.sh:
#   * the data folder is passed to the app as the machine sees it (C:\Users\Bob\.kawari is /mnt/c/Users/Bob/.kawari
#     there), because the containers the app makes through the API are bind-mounted from machine paths;
#   * the socket is the machine's own (rootless: /run/user/<uid>/podman/podman.sock; rootful: /run/podman/podman.sock),
#     found with `podman info`, not the Windows named pipe.
param(
    [string]$Image = $(if ($env:IMAGE) { $env:IMAGE } else { "kawari-web" }),
    [string]$Name = $(if ($env:NAME) { $env:NAME } else { "kawari-web" }),
    [int]$Port = $(if ($env:PORT) { [int]$env:PORT } else { 7301 }),
    [string]$DataDir = $(if ($env:DATA_DIR) { $env:DATA_DIR } else { Join-Path $env:USERPROFILE ".kawari" })
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Get-MachinePath([string]$Path) {
    # C:\Users\Bob\.kawari -> /mnt/c/Users/Bob/.kawari (the same rule as kawari_web/winpaths.py)
    $full = (Resolve-Path -LiteralPath $Path).Path
    if ($full -match '^([A-Za-z]):\\?(.*)$') {
        $rest = $Matches[2] -replace '\\', '/'
        return "/mnt/$($Matches[1].ToLower())/$rest".TrimEnd('/')
    }
    throw "$Path isn't on a drive letter; Podman's machine can't mount it."
}

if (-not (Get-Command podman -ErrorAction SilentlyContinue)) {
    throw "podman isn't installed or not on PATH. Install Podman for Windows (or Podman Desktop) first."
}
if (-not (Test-Path -LiteralPath $DataDir -PathType Container)) {
    throw "$DataDir does not exist. Set -DataDir to your Kawari config folder."
}

# The machine has to be running for anything else to work.
$machine = podman machine inspect 2>$null | ConvertFrom-Json
if (-not $machine) {
    throw "No Podman machine. Create one once: podman machine init   (then run this again)"
}
if ($machine[0].State -ne "running") {
    Write-Host "Starting the Podman machine..."
    podman machine start | Out-Host
}
$rootful = [bool]$machine[0].Rootful

# The API socket as the machine sees it, to mount into the container.
$socket = (podman info --format '{{.Host.RemoteSocket.Path}}' 2>$null)
if ($socket -and $socket.StartsWith("unix://")) { $socket = $socket.Substring(7) }
if (-not $socket -or -not $socket.StartsWith("/")) {
    $socket = if ($rootful) { "/run/podman/podman.sock" } else { "/run/user/1000/podman/podman.sock" }
}

$hostData = Get-MachinePath $DataDir

Write-Host "Building $Image..."
# The machine's default user is uid 1000; the image's user matches it so the mounted data folder is writable.
podman build -t $Image --build-arg UID=1000 --build-arg GID=1000 -f (Join-Path $here "Containerfile") $here
if ($LASTEXITCODE -ne 0) { throw "The image didn't build." }

podman rm -f $Name 2>$null | Out-Null

$runArgs = @(
    "run", "-d", "--name", $Name, "--restart", "unless-stopped",
    "--add-host=host.containers.internal:host-gateway",
    "-p", "${Port}:7301",
    # The CLI turns the Windows path into the machine's; the app gets the machine's form for the mounts it makes.
    "-v", "${DataDir}:/data",
    "-e", "KAWARI_HOST_DATA_DIR=$hostData",
    "-v", "${socket}:/run/podman.sock",
    "-e", "PODMAN_SOCKET=/run/podman.sock",
    "--security-opt", "label=disable"
)
# keep-id maps the machine user to the same uid inside, as on Linux; a rootful machine has no user namespace to keep.
if (-not $rootful) { $runArgs += "--userns=keep-id" }
$runArgs += $Image
& podman @runArgs | Out-Null
if ($LASTEXITCODE -ne 0) { throw "The container didn't start." }

Start-Sleep -Seconds 2
Write-Host ""
Write-Host "--- first log lines (should say the version and uid) ---"
podman logs $Name 2>&1 | Select-Object -First 12 | Out-Host
Write-Host "--------------------------------------------------------"
Write-Host "ArchonEngine ControlPanel is starting on http://127.0.0.1:$Port"
$lan = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.InterfaceAlias -notlike "vEthernet*" } |
    Select-Object -ExpandProperty IPAddress
foreach ($addr in $lan) { Write-Host "  from other machines: http://${addr}:$Port (allow the port in Windows Firewall)" }
Write-Host "Podman machine: $(if ($rootful) { 'rootful' } else { 'rootless' }), API socket $socket"
Write-Host "Host data dir passed through as KAWARI_HOST_DATA_DIR=$hostData"
Write-Host "First visit will ask you to create or adopt an administrator account."
