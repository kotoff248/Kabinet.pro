param(
    [int]$Port = 8001,

    [string]$HostName = "127.0.0.1",

    [int]$ReadyTimeoutSeconds = 15,

    [string]$CloudflaredPath = "",

    [switch]$KeepDjangoOnExit
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$DjangoServerScript = Join-Path $PSScriptRoot "django_server.ps1"

function Merge-CsvEnvironmentValue {
    param(
        [string]$CurrentValue,
        [string[]]$RequiredValues
    )

    $values = New-Object System.Collections.Generic.List[string]

    foreach ($item in ($CurrentValue -split ",")) {
        $value = $item.Trim()
        if ($value -and -not $values.Contains($value)) {
            $values.Add($value)
        }
    }

    foreach ($item in $RequiredValues) {
        $value = $item.Trim()
        if ($value -and -not $values.Contains($value)) {
            $values.Add($value)
        }
    }

    return ($values -join ",")
}

function Resolve-CloudflaredPath {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        if (Test-Path -LiteralPath $RequestedPath) {
            return (Resolve-Path -LiteralPath $RequestedPath).Path
        }
        throw "cloudflared not found at: $RequestedPath"
    }

    $command = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @()
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles "cloudflared\cloudflared.exe"
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} "cloudflared\cloudflared.exe"
    }
    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\cloudflared.exe"
    }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    if ($env:LOCALAPPDATA) {
        $packageRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
        if (Test-Path -LiteralPath $packageRoot) {
            $packageExe = Get-ChildItem -LiteralPath $packageRoot -Recurse -Filter cloudflared.exe -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($packageExe) {
                return $packageExe.FullName
            }
        }
    }

    throw "cloudflared is not installed or is not visible in PATH."
}

if (-not (Test-Path -LiteralPath $DjangoServerScript)) {
    throw "Django server helper not found: $DjangoServerScript"
}

$cloudflared = Resolve-CloudflaredPath -RequestedPath $CloudflaredPath

$env:DJANGO_ALLOWED_HOSTS = Merge-CsvEnvironmentValue `
    -CurrentValue $env:DJANGO_ALLOWED_HOSTS `
    -RequiredValues @("localhost", "127.0.0.1", ".trycloudflare.com")

$env:DJANGO_CSRF_TRUSTED_ORIGINS = Merge-CsvEnvironmentValue `
    -CurrentValue $env:DJANGO_CSRF_TRUSTED_ORIGINS `
    -RequiredValues @("https://*.trycloudflare.com")

$env:DJANGO_TRUST_X_FORWARDED_PROTO = "true"

Write-Output "Starting Kabinet.pro locally on http://$HostName`:$Port ..."
& $DjangoServerScript -Action restart -Port $Port -HostName $HostName -ReadyTimeoutSeconds $ReadyTimeoutSeconds

Write-Output ""
Write-Output "Starting Cloudflare Tunnel. Copy the https://*.trycloudflare.com link from the output below."
Write-Output "Press Ctrl+C to stop the public tunnel."
Write-Output ""

try {
    & $cloudflared tunnel --url "http://$HostName`:$Port"
} finally {
    if ($KeepDjangoOnExit) {
        Write-Output "Cloudflare Tunnel stopped. Django is still running on http://$HostName`:$Port."
    } else {
        Write-Output "Stopping local Django server..."
        & $DjangoServerScript -Action stop -Port $Port -HostName $HostName
    }
}
