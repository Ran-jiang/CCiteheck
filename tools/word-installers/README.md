# CCitecheck Word 公网测试版安装器

这些脚本用于给评委或小范围测试者安装连接 Render 公网服务的 Word 加载项。
测试者不需要安装 Python、Node.js，也不需要配置百炼或北大法宝密钥。

核查时文书内容会上传至部署方的 `https://cciteheck-api.onrender.com` 处理。请勿使用含有不适合上传至测试服务的敏感信息或个人信息的文书。

## Windows

1. 下载整个 `windows` 文件夹,保持 `.bat` 和 `.ps1` 在同一目录。(也可以直接从安装页 `https://cciteheck-api.onrender.com/install.html` 的"Windows 一键安装器"处分别下载两个文件,存到同一文件夹。)
2. 完全退出 Word。
3. 双击 `install-ccitecheck.bat`，并允许管理员权限。
4. 安装完成后打开 Word。
5. 进入“开始 → 加载项 → 高级 → 共享文件夹”，选择 CCitecheck 并点击“添加”。

如果 `.bat` 被安全策略拦截，请以管理员身份打开 Windows PowerShell，执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-ccitecheck.ps1
```

卸载时完全退出 Word，然后运行 `windows/uninstall-ccitecheck.ps1`。

可在 PowerShell 中先执行静态行为检查；该命令不会写文件、创建共享或修改注册表：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-ccitecheck.ps1 -DryRun
```

## macOS

1. 下载 `mac/install-ccitecheck.command`。
2. 完全退出 Word。
3. 双击 `install-ccitecheck.command`。如果 macOS 阻止打开，右键它并选择“打开”。

如果双击后提示没有执行权限，在终端进入下载目录并执行：

```bash
chmod +x install-ccitecheck.command
./install-ccitecheck.command
```

4. 重新打开 Word，在“开始 → 加载项”中打开 CCitecheck。

卸载时运行 `mac/uninstall-ccitecheck.command`。

## 限制

- Windows 安装脚本需要管理员权限，以创建本机 SMB 共享目录。
- 企业安全策略可能禁止 PowerShell、SMB 共享或自定义 Office 加载项。
- macOS 可能拦截从互联网下载的未签名脚本；可右键脚本并选择“打开”。
- 所有测试者共用部署者的 Render、百炼和北大法宝额度。
- Render 实例冷启动时首次连接可能需要约一分钟；安装器正在探活时请勿关闭窗口。
- 这些脚本适合测试分发，不替代 Microsoft AppSource 或 Microsoft 365 管理员集中部署。

## 故障速查

- 提示服务不可用：等待一分钟后重试，免费实例可能正在唤醒。
- Word 中没有加载项：完全退出 Word后重新打开，并重新检查安装步骤。
- Windows 无“共享文件夹”：检查企业策略是否禁止 SMB 或 Office 可信目录。
- 案例核查不可用：由部署方检查 Render 的 `PKULAW_ACCESS_TOKEN`，测试者无需配置密钥。
