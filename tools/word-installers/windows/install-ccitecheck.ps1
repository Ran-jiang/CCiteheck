[CmdletBinding()]
param([switch]$DryRun)

$ErrorActionPreference = "Stop"
$ManifestUrl = "https://raw.githubusercontent.com/Ran-jiang/check-7.16/main/apps/word_addin/manifest.render.xml"
$InstallRoot = Join-Path $env:ProgramData "CCitecheck\WordAddin"
$ManifestPath = Join-Path $InstallRoot "manifest.render.xml"
$ShareName = "CCitecheckAddins"
$CatalogUrl = "\\localhost\$ShareName"
$CatalogId = "{A8F4FDD5-AB97-4DBE-90A0-24CE1657868B}"
$CatalogKey = "HKCU:\Software\Microsoft\Office\16.0\WEF\TrustedCatalogs\$CatalogId"
$ExpectedManifestId = "b01e1d79-81a3-4162-9c0a-c80fa9b1203b"
$ExpectedHost = "https://cciteheck-api.onrender.com"

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Pause-BeforeExit {
    param([string]$Message)
    Write-Host ""
    Write-Host $Message
    Read-Host "Press Enter to close"
}

try {
    if (-not $DryRun -and -not (Test-Administrator)) {
        Write-Host "Requesting administrator permission..."
        $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $arguments
        exit 0
    }

    Write-Host "CCitecheck Word Add-in installer" -ForegroundColor Cyan
    Write-Host "================================"

    if (-not $DryRun -and (Get-Process -Name WINWORD -ErrorAction SilentlyContinue)) {
        Pause-BeforeExit "Microsoft Word is running. Close Word completely, then run this installer again."
        exit 1
    }

    if ($DryRun) {
        Write-Host "Dry run: no files, shares, or registry entries will be changed."
        $health = Invoke-RestMethod -Uri "$ExpectedHost/api/health" -Method Get -TimeoutSec 45
        if ($health.status -ne "ok") { throw "The public service did not pass its health check." }
        Write-Host "Dry run completed successfully." -ForegroundColor Green
        return
    }

    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null

    Write-Host "[1/4] Downloading the public manifest..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $ManifestUrl -OutFile $ManifestPath -UseBasicParsing

    $manifest = Get-Content -Path $ManifestPath -Raw
    if (-not $manifest.Contains($ExpectedManifestId) -or -not $manifest.Contains($ExpectedHost)) {
        throw "The downloaded manifest failed validation. Installation was stopped."
    }

    Write-Host "[2/4] Creating the local Office catalog..."
    $existingShare = Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue
    if ($existingShare -and $existingShare.Path -ne $InstallRoot) {
        Remove-SmbShare -Name $ShareName -Force
        $existingShare = $null
    }
    if (-not $existingShare) {
        $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        New-SmbShare -Name $ShareName -Path $InstallRoot -ReadAccess $currentUser | Out-Null
    }

    Write-Host "[3/4] Registering the catalog with Microsoft Office..."
    New-Item -Path $CatalogKey -Force | Out-Null
    New-ItemProperty -Path $CatalogKey -Name "Id" -Value $CatalogId -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $CatalogKey -Name "Url" -Value $CatalogUrl -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $CatalogKey -Name "Flags" -Value 1 -PropertyType DWord -Force | Out-Null

    Write-Host "[4/4] Checking the public service..."
    $health = Invoke-RestMethod -Uri "$ExpectedHost/api/health" -Method Get -TimeoutSec 45
    if ($health.status -ne "ok") {
        throw "The public CCitecheck service did not pass its health check."
    }

    Write-Host ""
    Write-Host "Installation completed successfully." -ForegroundColor Green
    Write-Host "Next: open Word, then choose Home > Add-ins > Advanced > Shared Folder > CCitecheck > Add."
    Pause-BeforeExit "The CCitecheck catalog is ready."
}
catch {
    Write-Host ""
    Write-Host "Installation failed: $($_.Exception.Message)" -ForegroundColor Red
    Pause-BeforeExit "No API keys were changed. You can safely run the installer again."
    exit 1
}
