param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "status",

    [int]$Port = 8001,

    [string]$HostName = "127.0.0.1",

    [int]$ReadyTimeoutSeconds = 10,

    [string]$PythonPath = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$RunDir = Join-Path $RepoRoot ".run"
$PidFile = Join-Path $RunDir "django-$Port.pid"
$OutLog = Join-Path $RunDir "django-$Port.out.log"
$ErrLog = Join-Path $RunDir "django-$Port.err.log"

function Get-ListenerPid {
    param([int]$Port, [string]$HostName)

    try {
        $connection = Get-NetTCPConnection -LocalAddress $HostName -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -First 1
        if ($connection) {
            return [int]$connection.OwningProcess
        }
    } catch {
        # Fall back to netstat below for older shells or restricted environments.
    }

    $escapedAddress = [regex]::Escape("$HostName`:$Port")
    $line = netstat -ano -p TCP | Select-String -Pattern "^\s*TCP\s+$escapedAddress\s+\S+\s+LISTENING\s+(\d+)" |
        Select-Object -First 1
    if ($line -and $line.Matches.Count -gt 0) {
        return [int]$line.Matches[0].Groups[1].Value
    }

    return $null
}

function Stop-ProcessTree {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return
    }

    try {
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction Stop
        foreach ($child in $children) {
            Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
        }
    } catch {
        # Process tree lookup is best-effort; the direct PID stop below is enough
        # for listener PIDs and works in more restricted shells.
    }

    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Get-StarterPid {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return $null
    }

    $raw = (Get-Content -LiteralPath $PidFile -Raw -ErrorAction SilentlyContinue).Trim()
    if ($raw -match "^\d+$") {
        return [int]$raw
    }

    return $null
}

function Stop-Server {
    $listenerPid = Get-ListenerPid -Port $Port -HostName $HostName
    $starterPid = Get-StarterPid
    $pids = @()

    if ($listenerPid) {
        $pids += $listenerPid
    }
    if ($starterPid) {
        $pids += $starterPid
    }

    $pids = $pids | Sort-Object -Unique
    foreach ($processIdToStop in $pids) {
        Stop-ProcessTree -ProcessId $processIdToStop
    }

    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue

    if ($pids.Count -eq 0) {
        Write-Output "No Django server found on $HostName`:$Port."
    } else {
        Write-Output "Stopped Django server on $HostName`:$Port (PIDs: $($pids -join ', '))."
    }
}

function Start-Server {
    $listenerPid = Get-ListenerPid -Port $Port -HostName $HostName
    if ($listenerPid) {
        Write-Output "Django server is already listening on $HostName`:$Port (PID $listenerPid)."
        return
    }

    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

    $resolvedPython = if ([System.IO.Path]::IsPathRooted($PythonPath)) {
        $PythonPath
    } else {
        Join-Path $RepoRoot $PythonPath
    }
    if (-not (Test-Path -LiteralPath $resolvedPython)) {
        throw "Python not found: $resolvedPython"
    }

    $launchPython = if ([System.IO.Path]::IsPathRooted($PythonPath)) {
        $resolvedPython
    } else {
        $PythonPath
    }

    # Keep redirection inside cmd.exe. PowerShell's Start-Process redirection can
    # keep the Codex shell request open while the long-lived Django child runs.
    $cmdLine = "call `"$launchPython`" manage.py runserver $HostName`:$Port --noreload 1> .run\django-$Port.out.log 2> .run\django-$Port.err.log"
    $process = Start-Process `
        -FilePath $env:ComSpec `
        -ArgumentList @("/d", "/c", $cmdLine) `
        -WorkingDirectory $RepoRoot `
        -PassThru `
        -WindowStyle Hidden

    Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ASCII

    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $listenerPid = Get-ListenerPid -Port $Port -HostName $HostName
        if ($listenerPid) {
            Write-Output "Django server is listening on http://$HostName`:$Port (starter PID $($process.Id), listener PID $listenerPid)."
            return
        }
    } while ((Get-Date) -lt $deadline)

    Stop-Server
    Write-Error "Django server did not become ready on $HostName`:$Port within $ReadyTimeoutSeconds seconds. Check $ErrLog."
}

function Show-Status {
    $listenerPid = Get-ListenerPid -Port $Port -HostName $HostName
    $starterPid = Get-StarterPid

    if ($listenerPid) {
        Write-Output "Django server is listening on http://$HostName`:$Port (listener PID $listenerPid, starter PID $starterPid)."
    } else {
        Write-Output "No Django server is listening on $HostName`:$Port."
        if ($starterPid) {
            Write-Output "Stale starter PID file exists: $PidFile -> $starterPid."
        }
    }
}

switch ($Action) {
    "start" {
        Start-Server
    }
    "stop" {
        Stop-Server
    }
    "restart" {
        Stop-Server
        Start-Server
    }
    "status" {
        Show-Status
    }
}
