# CCiteheck 本地完整服务安装器（Windows，需管理员：SMB 共享与证书信任）
$ErrorActionPreference = "Stop"

$Src = $PSScriptRoot
$InstallDir = Join-Path $env:LOCALAPPDATA "CCiteheck"
$ShareName = "CCitecheckAddins"
$CatalogId = "{5C4F02F1-9A47-4E62-8D5B-CC17E6A30B21}"
$CatalogKey = "HKCU:\Software\Microsoft\Office\16.0\WEF\TrustedCatalogs\$CatalogId"

function Fail([string]$msg) {
    Write-Host ""
    Write-Host "安装失败：$msg" -ForegroundColor Red
    Write-Host "可重新运行本安装器；已有安装不会损坏。"
    exit 1
}

Write-Host "CCiteheck 本地服务安装器" -ForegroundColor Cyan
Write-Host "========================"

# 1. 预检
# 注意：全程避免 "原生命令 2>$null" 写法——ErrorActionPreference=Stop 下
# 原生命令的 stderr 重定向会被包装成异常直接中断脚本
if (Get-Process -Name WINWORD -ErrorAction SilentlyContinue) {
    Fail "Microsoft Word 正在运行，请完全退出 Word 后重新运行安装器。"
}
$oldInstall = Get-ScheduledTask -TaskName "CCiteheck-API" -ErrorAction SilentlyContinue
foreach ($port in 3000, 3010) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn -and -not $oldInstall) {
        $owner = (Get-Process -Id $conn[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
        Fail "端口 $port 已被进程「$owner」占用，请先释放该端口。"
    }
}

# 2. 停旧服务
foreach ($t in "CCiteheck-API", "CCiteheck-EurLex") {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
    }
}
Get-Process pythonw, node -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "$InstallDir*" } |
    Stop-Process -Force -ErrorAction SilentlyContinue

# 3. 拷贝 payload（升级时保留 .env——必须按字节复制，
#    Get/Set-Content 文本往返会以 ANSI 编码写坏 UTF-8 配置文件）
Write-Host "[1/6] 安装文件到 $InstallDir ..."
$keepEnvFile = $null
if (Test-Path (Join-Path $InstallDir ".env")) {
    $keepEnvFile = Join-Path $env:TEMP "ccitecheck-env-backup"
    Copy-Item (Join-Path $InstallDir ".env") $keepEnvFile -Force
}
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Copy-Item -Path (Join-Path $Src "payload\*") -Destination $InstallDir -Recurse -Force
New-Item -ItemType Directory -Path (Join-Path $InstallDir "logs") -Force | Out-Null
if ($keepEnvFile) {
    # 本版随包分发的 .env 先存一份，供合并新增配置项
    $pkgEnvFile = Join-Path $env:TEMP "ccitecheck-env-package"
    if (Test-Path (Join-Path $InstallDir ".env")) {
        Copy-Item (Join-Path $InstallDir ".env") $pkgEnvFile -Force
    }
    Copy-Item $keepEnvFile (Join-Path $InstallDir ".env") -Force
    # 保留既有取值，同时补入本版新增的配置项，避免新增密钥进不到老安装
    $mergeFrom = if (Test-Path $pkgEnvFile) { $pkgEnvFile } else { Join-Path $InstallDir ".env.template" }
    if (Test-Path $mergeFrom) {
        $have = @(Get-Content (Join-Path $InstallDir ".env") | ForEach-Object {
            if ($_ -match '^([A-Z_][A-Z0-9_]*)=') { $Matches[1] } })
        $add = @(Get-Content $mergeFrom | Where-Object {
            $_ -match '^([A-Z_][A-Z0-9_]*)=' -and ($have -notcontains $Matches[1]) })
        if ($add.Count -gt 0) {
            Add-Content (Join-Path $InstallDir ".env") (@("", "# 本次升级新增的配置项") + $add)
            Write-Host "  已合并 $($add.Count) 项新增配置"
        }
    }
    Remove-Item $keepEnvFile -Force -ErrorAction SilentlyContinue
    Remove-Item $pkgEnvFile -Force -ErrorAction SilentlyContinue
} elseif (-not (Test-Path (Join-Path $InstallDir ".env"))) {
    Copy-Item (Join-Path $InstallDir ".env.template") (Join-Path $InstallDir ".env")
    Write-Host "提示：包内未含密钥，已用模板生成 .env——语义核查需要填入 DASHSCOPE_API_KEY。" -ForegroundColor Yellow
}
Copy-Item (Join-Path $Src "uninstall.ps1") (Join-Path $InstallDir "uninstall.ps1") -Force

# 4. HTTPS 开发证书（10 年）
Write-Host "[2/6] 安装并信任 localhost HTTPS 证书..."
& (Join-Path $InstallDir "runtime\node\node.exe") `
    (Join-Path $InstallDir "vendor\certs\node_modules\office-addin-dev-certs\cli.js") `
    install --days 365
if ($LASTEXITCODE -ne 0) { Fail "HTTPS 证书安装未完成" }

# 5. 注册并启动常驻服务（计划任务，登录自启+失败重启+隐藏窗口）
Write-Host "[3/6] 注册开机自启服务..."
foreach ($svc in @(@{n="CCiteheck-API"; t="task-api.xml.tmpl"}, @{n="CCiteheck-EurLex"; t="task-eurlex.xml.tmpl"})) {
    $xml = (Get-Content (Join-Path $InstallDir $svc.t) -Raw) -replace "__ROOT__", $InstallDir
    try {
        Register-ScheduledTask -TaskName $svc.n -Xml $xml -Force | Out-Null
        Start-ScheduledTask -TaskName $svc.n
    } catch {
        Fail "计划任务 $($svc.n) 注册或启动失败：$($_.Exception.Message)"
    }
}

# 6. 就绪检查：直接测端口是否在监听（TCP 连接），绕开 PowerShell 5.1 的
# HTTPS 客户端在本机开发证书、TLS、localhost 解析上的各种坑。
function Test-PortReady([int]$port, [int]$tries) {
    foreach ($i in 1..$tries) {
        Start-Sleep -Seconds 1
        try {
            $client = New-Object System.Net.Sockets.TcpClient
            $client.Connect("127.0.0.1", $port)
            if ($client.Connected) { $client.Close(); return $true }
        } catch {}
    }
    return $false
}
Write-Host "[4/6] 等待服务就绪..."
if (-not (Test-PortReady 3000 30)) {
    Write-Host "--- api.log 末尾 ---" -ForegroundColor Yellow
    if (Test-Path "$InstallDir\logs\api.log") { Get-Content "$InstallDir\logs\api.log" -Tail 20 }
    Fail "API 服务未在 30 秒内就绪，完整日志见 $InstallDir\logs\api.log"
}
if (-not (Test-PortReady 3010 15)) {
    Write-Host "警告：EUR-Lex 服务未就绪（不影响国内法规核查），日志见 $InstallDir\logs\eurlex.log" -ForegroundColor Yellow
}

# 7. Word 加载项（本机只读共享 manifest 目录 + 受信任目录注册）
Write-Host "[5/6] 安装 Word 加载项..."
$catalogDir = Join-Path $InstallDir "wef-catalog"
New-Item -ItemType Directory -Path $catalogDir -Force | Out-Null
Copy-Item (Join-Path $InstallDir "apps\word_addin\manifest.xml") (Join-Path $catalogDir "ccitecheck-manifest.xml") -Force
$existingShare = Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue
if ($existingShare -and $existingShare.Path -ne $catalogDir) {
    Remove-SmbShare -Name $ShareName -Force
    $existingShare = $null
}
if (-not $existingShare) {
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    New-SmbShare -Name $ShareName -Path $catalogDir -ReadAccess $currentUser | Out-Null
}
New-Item -Path $CatalogKey -Force | Out-Null
New-ItemProperty -Path $CatalogKey -Name "Id" -Value $CatalogId -PropertyType String -Force | Out-Null
New-ItemProperty -Path $CatalogKey -Name "Url" -Value "\\localhost\$ShareName" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $CatalogKey -Name "Flags" -Value 1 -PropertyType DWord -Force | Out-Null

# 8. 自检
Write-Host "[6/6] 环境自检："
& (Join-Path $InstallDir "bin\run-doctor.cmd")

Write-Host ""
Write-Host "安装完成！" -ForegroundColor Green
Write-Host "· Word：开始 → 加载项 → 高级 → 共享文件夹 → CCiteheck 法律引用核查 → 添加"
Write-Host "· 网页版：https://localhost:3000（即将自动打开）"
Write-Host "· 卸载：以管理员运行 $InstallDir\uninstall.ps1"
Start-Process "https://localhost:3000"
