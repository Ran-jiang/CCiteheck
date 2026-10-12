# CCiteheck 卸载器（Windows，需管理员）
$ErrorActionPreference = "SilentlyContinue"

$InstallDir = Join-Path $env:LOCALAPPDATA "CCiteheck"
$ShareName = "CCitecheckAddins"
$CatalogKey = "HKCU:\Software\Microsoft\Office\16.0\WEF\TrustedCatalogs\{5C4F02F1-9A47-4E62-8D5B-CC17E6A30B21}"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

Write-Host "CCiteheck 卸载器" -ForegroundColor Cyan
Write-Host "================"

Write-Host "[1/4] 停止并移除常驻服务..."
foreach ($t in "CCiteheck-API", "CCiteheck-EurLex") {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
    }
}
Get-Process pythonw, node | Where-Object { $_.Path -like "$InstallDir*" } | Stop-Process -Force

Write-Host "[2/4] 移除 Word 加载项目录与注册..."
Remove-SmbShare -Name $ShareName -Force
Remove-Item -Path $CatalogKey -Recurse -Force

Write-Host "[3/4] localhost HTTPS 开发证书默认保留。"
$yn = Read-Host "是否同时移除证书？(y/N)"
if ($yn -eq "y") {
    & (Join-Path $InstallDir "runtime\node\node.exe") `
        (Join-Path $InstallDir "vendor\certs\node_modules\office-addin-dev-certs\cli.js") uninstall
}

Write-Host "[4/4] 删除安装目录 $InstallDir ..."
$yn = Read-Host "确认删除全部程序文件（含 .env 与日志）？(y/N)"
if ($yn -eq "y") {
    Set-Location $env:TEMP
    Remove-Item -Path $InstallDir -Recurse -Force
    Write-Host "已删除。"
} else {
    Write-Host "已保留程序目录，仅停用了服务与 Word 加载项。"
}
Read-Host "按回车关闭"
