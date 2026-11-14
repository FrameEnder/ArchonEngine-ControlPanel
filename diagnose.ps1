# ArchonEngine ControlPanel diagnostics on Windows - "which build is running, and can it reach what it needs"
# (the Windows branch's diagnose.sh).
param(
    [string]$Name = $(if ($env:NAME) { $env:NAME } else { "kawari-web" }),
    [string]$Image = $(if ($env:IMAGE) { $env:IMAGE } else { "kawari-web" }),
    [int]$Port = $(if ($env:PORT) { [int]$env:PORT } else { 7301 }),
    [string]$DataDir = $(if ($env:DATA_DIR) { $env:DATA_DIR } else { Join-Path $env:USERPROFILE ".kawari" })
)

function Section($title) { Write-Host ""; Write-Host "=== $title ===" }
function Probe($url) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 4
        Write-Host "  $url -> $($r.StatusCode)"
    } catch {
        Write-Host "  $url FAILED: $($_.Exception.Message)"
    }
}

Section "host"
Write-Host "user: $env:USERNAME"
Write-Host "data dir: $DataDir $(if (Test-Path -LiteralPath $DataDir) { '[exists]' } else { '[MISSING]' })"
Write-Host "web dir: $(if (Test-Path -LiteralPath (Join-Path $DataDir 'web')) { 'exists' } else { 'absent (normal before the first start)' })"

Section "settings.conf paths vs reality"
$conf = Join-Path $DataDir "settings.conf"
if (Test-Path -LiteralPath $conf) {
    foreach ($key in "DATA_DIR", "GAME_PATH") {
        $line = Select-String -LiteralPath $conf -Pattern "^$key=" | Select-Object -First 1
        $val = if ($line) { ($line.Line -split "=", 2)[1].Trim('"', "'") } else { "" }
        if (-not $val) { Write-Host "  ${key}: (not set)" }
        elseif ($val -match '^[A-Za-z]:') {
            Write-Host "  ${key}: $val  [$(if (Test-Path -LiteralPath $val) { 'exists' } else { 'MISSING on this machine' })]"
        } else {
            Write-Host "  ${key}: $val  [a machine path; the ControlPanel reads Windows paths too]"
        }
    }
    foreach ($db in "world.db", "login.db") {
        $f = Join-Path (Join-Path $DataDir "data") $db
        if (Test-Path -LiteralPath $f) { Write-Host "  $f ($([math]::Round((Get-Item $f).Length / 1MB, 1)) MiB)" }
        else { Write-Host "  $f absent" }
    }
} else {
    Write-Host "  no settings.conf at $conf"
}

Section "podman and its machine"
if (-not (Get-Command podman -ErrorAction SilentlyContinue)) { Write-Host "  podman is not on PATH"; exit 1 }
podman version --format '  client {{.Client.Version}}, server {{.Server.Version}}' 2>&1 | Out-Host
$machine = podman machine inspect 2>$null | ConvertFrom-Json
if ($machine) {
    $m = $machine[0]
    Write-Host "  machine: $($m.Name), $($m.State), $(if ($m.Rootful) { 'rootful' } else { 'rootless' })"
    Write-Host "  API socket in the machine: $(podman info --format '{{.Host.RemoteSocket.Path}}' 2>$null)"
} else {
    Write-Host "  NO machine: podman machine init; podman machine start"
}

Section "containers named or imaged like $Name"
podman ps -a --filter "name=$Name" --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.CreatedAt}}' 2>&1 | Out-Host
podman ps -a --filter "ancestor=$Image" --format '  {{.Names}} ({{.Status}})' 2>&1 | Out-Host

Section "scheduled task"
$task = Get-ScheduledTask -TaskName "ArchonEngine ControlPanel" -ErrorAction SilentlyContinue
if ($task) { Write-Host "  present, $($task.State)" } else { Write-Host "  none (install-service.ps1 makes it)" }

Section "image"
podman image inspect $Image --format '  id: {{.Id}}{{"\n"}}  created: {{.Created}}' 2>$null | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Host "  image $Image not built" }

Section "what the running container sees"
podman container exists $Name
if ($LASTEXITCODE -eq 0) {
    Write-Host "  userns: $(podman inspect $Name --format '{{.HostConfig.UsernsMode}}')"
    Write-Host "  KAWARI_HOST_DATA_DIR: $(podman exec $Name sh -c 'echo $KAWARI_HOST_DATA_DIR' 2>$null)"
    podman exec $Name sh -c 'id; ls -ld /data /data/web 2>&1' 2>$null | ForEach-Object { "  $_" } | Out-Host
    Write-Host "  container can talk to the API socket:"
    podman exec $Name python -c @"
import http.client, socket
class C(http.client.HTTPConnection):
    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect('/run/podman.sock'); self.sock = s
try:
    c = C('localhost'); c.request('GET', '/v5.0.0/libpod/info'); print('    OK', c.getresponse().status)
except Exception as exc:
    print('    FAILED:', exc)
"@ 2>$null | Out-Host
    Write-Host "  app answers inside the container:"
    podman exec $Name python -c "import urllib.request; print('    ', urllib.request.urlopen('http://127.0.0.1:7301/connect', timeout=4).status)" 2>&1 | Out-Host
} else {
    Write-Host "  no container called $Name (run .\run.ps1)"
}

Section "is it reachable?"
podman port $Name 2>$null | ForEach-Object { "  $_" } | Out-Host
Probe "http://127.0.0.1:$Port/connect"
$lan = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.InterfaceAlias -notlike "vEthernet*" } |
    Select-Object -ExpandProperty IPAddress
foreach ($addr in $lan) { Probe "http://${addr}:$Port/connect" }
Write-Host "  Use 127.0.0.1 rather than localhost: localhost can resolve to ::1 first."

Section "Windows Firewall"
$rule = Get-NetFirewallRule -DisplayName "ArchonEngine ControlPanel" -ErrorAction SilentlyContinue
if ($rule) { Write-Host "  port $Port allowed ($($rule.Profile))" }
else { Write-Host "  no rule for ${Port}: other machines can't reach it (install-service.ps1 -Firewall, as administrator)" }

Section "last 15 log lines"
podman logs --tail 15 $Name 2>&1 | ForEach-Object { "  $_" } | Out-Host
