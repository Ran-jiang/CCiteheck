[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$InstallRoot = Join-Path $env:ProgramData "CCitecheck\WordAddin"
$ShareName = "CCitecheckAddins"
$CatalogId = "{A8F4FDD5-AB97-4DBE-90A0-24CE1657868B}"
$CatalogKey = "HKCU:\Software\Microsoft\Office\16.0\WEF\TrustedCatalogs\$CatalogId"

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Administrator)) {
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $arguments
    exit 0
}

try {
    if (Get-Process -Name WINWORD -ErrorAction SilentlyContinue) {
        throw "Close Microsoft Word completely before uninstalling."
    }

    if (Test-Path $CatalogKey) {
        Remove-Item -Path $CatalogKey -Recurse -Force
    }
    if (Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue) {
        Remove-SmbShare -Name $ShareName -Force
    }
    if (Test-Path $InstallRoot) {
        Remove-Item -Path $InstallRoot -Recurse -Force
    }

    Write-Host "CCitecheck Word Add-in catalog was removed." -ForegroundColor Green
}
catch {
    Write-Host "Uninstall failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
finally {
    Read-Host "Press Enter to close"
}
