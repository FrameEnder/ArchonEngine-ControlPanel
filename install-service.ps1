# Keep ArchonEngine ControlPanel running on Windows: back after a crash, a log-off and a reboot (the Windows branch's
# install-service.sh). Run run.ps1 once first: it builds the image and creates the container.
#
#   .\install-service.ps1               install
#   .\install-service.ps1 -Firewall     also allow the ControlPanel and game ports through Windows Firewall
#                                       (run PowerShell as administrator for that)
#   .\install-service.ps1 -Remove       remove the scheduled task (the container, image and data are left alone)
#
# Linux has systemd and Quadlet for this; Windows has neither, so:
#   * a scheduled task starts the Podman machine when you log on (Podman's VM doesn't start by itself), and starts the
#     ControlPanel's container in case it was stopped;
#   * the machine's podman-restart service is enabled, so every container with a restart policy (the ControlPanel's
#     and the game server's, both "unless-stopped") comes back when the machine starts;
#   * a container that crashes while the machine runs is restarted by Podman itself (its restart policy).
param(
    [string]$Name = $(if ($env:NAME) { $env:NAME } else { "kawari-web" }),
    [int]$Port = $(if ($env:PORT) { [int]$env:PORT } else { 7301 }),
    [switch]$Firewall,
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$task = "ArchonEngine ControlPanel"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "  - scheduled task removed; the container, image and data were left alone"
    Write-Host "    (podman rm -f $Name removes the container)"
    exit 0
}

$podman = (Get-Command podman -ErrorAction Stop).Source
podman container exists $Name
if ($LASTEXITCODE -ne 0) { throw "No $Name container yet: run .\run.ps1 first." }

$machine = (podman machine inspect | ConvertFrom-Json)[0]
if ($machine.State -ne "running") { podman machine start | Out-Host }

# Containers with a restart policy come back when the machine boots.
if ($machine.Rootful) {
    podman machine ssh "sudo systemctl enable podman-restart.service" | Out-Host
} else {
    podman machine ssh "systemctl --user enable podman-restart.service; sudo loginctl enable-linger `$USER" | Out-Host
}
Write-Host "  - podman-restart enabled in the machine"

# At log-on: start the machine, then the ControlPanel (a no-op if podman-restart already did).
$command = "& '$podman' machine start 2>`$null; Start-Sleep -Seconds 5; & '$podman' start $Name"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$command`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings `
    -Description "Starts Podman's machine and the $Name container (ArchonEngine ControlPanel)" -Force | Out-Null
Write-Host "  - scheduled task '$task' starts them when you log on"

if ($Firewall) {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { throw "-Firewall needs PowerShell run as administrator." }
    # The ControlPanel, then the ports clients use (kawari_core/constants.py's SERVICES: Frontier 21058, Lobby 21059,
    # Login 21060, Patch 21061, Web 21062, World 21063; Admin 21057 stays local unless exposed on purpose).
    $rules = @(
        @{ Name = "ArchonEngine ControlPanel"; Ports = "$Port" },
        @{ Name = "ArchonEngine game server"; Ports = "21058-21063" }
    )
    foreach ($rule in $rules) {
        Remove-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
        New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Protocol TCP `
            -LocalPort ($rule.Ports -split ",") -Action Allow -Profile Private | Out-Null
        Write-Host "  - Windows Firewall: $($rule.Name) allowed on private networks ($($rule.Ports))"
    }
}
Write-Host ""
Write-Host "Done. Log off and on (or reboot) to check it comes back by itself."
