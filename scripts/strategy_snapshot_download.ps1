[CmdletBinding()]
param(
    [switch]$ValidateOnly,

    [string]$ConnectionConfig = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Builder = Join-Path $ProjectRoot "strategy_snapshot_builder.py"
$ConfigFile = if ($ConnectionConfig) { $ConnectionConfig } else { Join-Path $ProjectRoot "cache\strict_history_upload_config.json" }
$LatestResult = Join-Path $ProjectRoot "cache\strategy_snapshot_generate_latest.json"
$OutputDirectory = Join-Path $ProjectRoot "output"
$LocalArchive = Join-Path $OutputDirectory "strategy_snapshot_latest.zip"
$LocalModule = Join-Path $OutputDirectory "聚宽策略快照.py"
$LocalBacktest = Join-Path $OutputDirectory "聚宽原生回测策略.py"
$LocalStrictExport = Join-Path $OutputDirectory "聚宽严格历史导出.py"

function Write-Step {
    param([int]$Number, [string]$Text)
    Write-Host ""
    Write-Host ("[{0}/5] {1}" -f $Number, $Text) -ForegroundColor Cyan
}

function Stop-Friendly {
    param([string]$Message, [int]$Code = 1)
    Write-Host ""
    Write-Host "没有完成：$Message" -ForegroundColor Red
    exit $Code
}

function Invoke-NativeCapture {
    param([string]$FilePath, [string[]]$Arguments)
    $output = @(& $FilePath @Arguments 2>&1 | ForEach-Object { $_.ToString() })
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Text = ($output -join [Environment]::NewLine)
    }
}

function Convert-JsonResult {
    param([string]$Text, [string]$Label)
    try {
        return $Text | ConvertFrom-Json
    }
    catch {
        Stop-Friendly "$Label 返回了无法识别的结果。请把窗口内容截图给我。"
    }
}

function Save-LatestResult {
    param([object]$Result)
    $directory = Split-Path -Parent $LatestResult
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $temporary = "$LatestResult.tmp"
    $json = $Result | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText($temporary, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $LatestResult -Force
}

function Extract-SnapshotMember {
    param([string]$Archive, [string]$Member, [string]$Destination)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $entries = @($zip.Entries | Where-Object { $_.FullName -eq $Member })
        if ($entries.Count -ne 1) {
            Stop-Friendly "快照包中没有唯一的 $Member。"
        }
        $temporary = "$Destination.tmp"
        $input = $entries[0].Open()
        try {
            $output = [System.IO.File]::Open($temporary, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            try {
                $input.CopyTo($output)
                $output.Flush()
            }
            finally {
                $output.Dispose()
            }
        }
        finally {
            $input.Dispose()
        }
        Move-Item -LiteralPath $temporary -Destination $Destination -Force
    }
    finally {
        $zip.Dispose()
    }
}

Write-Step 1 "检查本机和服务器连接配置"
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        Stop-Friendly "电脑上没有找到 Python。"
    }
    $python = $pythonCommand.Source
}
if (-not (Test-Path -LiteralPath $Builder -PathType Leaf)) {
    Stop-Friendly "本地缺少策略快照校验器：$Builder"
}
if (-not (Test-Path -LiteralPath $ConfigFile -PathType Leaf)) {
    Stop-Friendly "缺少本机服务器连接配置：$ConfigFile"
}
try {
    $connection = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    Stop-Friendly "本机服务器连接配置无法读取。"
}
$sshTarget = [string]$connection.ssh_target
$identityFile = [Environment]::ExpandEnvironmentVariables([string]$connection.identity_file)
$remoteProject = [string]$connection.remote_project
if ($sshTarget -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$') {
    Stop-Friendly "服务器地址不符合安全规则。"
}
if ($remoteProject -notmatch '^/[A-Za-z0-9._/-]+$' -or $remoteProject.Contains("..")) {
    Stop-Friendly "服务器项目路径不符合安全规则。"
}
if (-not (Test-Path -LiteralPath $identityFile -PathType Leaf)) {
    Stop-Friendly "找不到服务器密钥。脚本不会修改或重置密钥。"
}
$sshCommand = Get-Command ssh -ErrorAction SilentlyContinue
$scpCommand = Get-Command scp -ErrorAction SilentlyContinue
if ($null -eq $sshCommand -or $null -eq $scpCommand) {
    Stop-Friendly "电脑上没有找到 Windows OpenSSH（ssh/scp）。"
}
$compile = Invoke-NativeCapture $python @(
    "-m", "py_compile",
    $Builder,
    (Join-Path $ProjectRoot "strategy_snapshot_runtime.py"),
    (Join-Path $ProjectRoot "joinquant_point_in_time.py"),
    (Join-Path $ProjectRoot "joinquant_strict_history_exporter.py")
)
if ($compile.ExitCode -ne 0) {
    Stop-Friendly "本地策略快照工具无法运行。`n$($compile.Text)"
}
Write-Host "本机检查通过。" -ForegroundColor Green
if ($ValidateOnly) {
    Write-Host ""
    Write-Host "本地入口检查通过（没有连接服务器、没有生成快照）。" -ForegroundColor Green
    exit 0
}

$sshOptions = @(
    "-i", $identityFile,
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
    "-o", "StrictHostKeyChecking=yes"
)

Write-Step 2 "读取服务器当前实际运行的策略"
$remoteCommand = "cd $remoteProject && set -a && . ./stock-analysis.env && set +a && .venv/bin/python strategy_snapshot_builder.py build"
$build = Invoke-NativeCapture $sshCommand.Source @($sshOptions + @($sshTarget, $remoteCommand))
$result = Convert-JsonResult $build.Text "服务器策略快照程序"
if ($build.ExitCode -ne 0 -or $result.status -ne "success") {
    $reason = [string]$result.error
    switch -Wildcard ($reason) {
        "*ACTIVE_SOURCE_NEWER_THAN_SERVICE*" { $reason = "服务器策略文件比正在运行的服务更新。为避免快照版本错误，已停止；需要先在非交易时段完成受控重启。" }
        "*STRATEGY_SERVICE_NOT_RUNNING*" { $reason = "服务器策略服务当前没有运行，不能确认实际生效版本。" }
        "*SENSITIVE_ENVIRONMENT_VALUE_DETECTED*" { $reason = "安全扫描发现快照可能包含敏感值，已拒绝生成。" }
        "*RETENTION_LIMIT*" { $reason = "服务器已保留 24 个不同策略快照，需要人工核对后再扩展。" }
        default { if (-not $reason) { $reason = "服务器没有返回具体原因。" } }
    }
    Stop-Friendly $reason
}
$remoteArchive = [string]$result.archive
$expectedPrefix = "$remoteProject/cache/backtest/strategy_snapshots/strategy-snapshot-"
if (-not $remoteArchive.StartsWith($expectedPrefix) -or -not $remoteArchive.EndsWith(".zip") -or $remoteArchive.Contains("..")) {
    Stop-Friendly "服务器返回的快照路径不符合安全规则。"
}
Write-Host "已冻结服务器当前运行版本。" -ForegroundColor Green
Write-Host "快照 ID：$($result.snapshot_id)"

Write-Step 3 "下载不含密钥的策略快照"
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$temporaryArchive = Join-Path $OutputDirectory ".strategy_snapshot_latest.downloading.zip"
Remove-Item -LiteralPath $temporaryArchive -Force -ErrorAction SilentlyContinue
$download = Invoke-NativeCapture $scpCommand.Source @(
    $sshOptions + @("${sshTarget}:$remoteArchive", $temporaryArchive)
)
if ($download.ExitCode -ne 0) {
    Remove-Item -LiteralPath $temporaryArchive -Force -ErrorAction SilentlyContinue
    Stop-Friendly "快照下载失败。服务器策略和本地旧快照都没有变化。`n$($download.Text)"
}
$downloadSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $temporaryArchive).Hash.ToLowerInvariant()
if ($downloadSha256 -ne ([string]$result.package_sha256).ToLowerInvariant()) {
    Remove-Item -LiteralPath $temporaryArchive -Force -ErrorAction SilentlyContinue
    Stop-Friendly "下载后的文件指纹与服务器不一致，已删除不完整临时文件。"
}
Write-Host "下载完成，服务器与电脑文件指纹一致。" -ForegroundColor Green

Write-Step 4 "在电脑上复核快照"
$verify = Invoke-NativeCapture $python @($Builder, "verify", "--package", $temporaryArchive)
$verified = Convert-JsonResult $verify.Text "本地策略快照校验器"
if ($verify.ExitCode -ne 0 -or $verified.accepted -ne $true) {
    Remove-Item -LiteralPath $temporaryArchive -Force -ErrorAction SilentlyContinue
    Stop-Friendly "策略快照没有通过本地复核，未发布到 output。`n$($verify.Text)"
}
if ([string]$verified.snapshot_id -ne [string]$result.snapshot_id) {
    Remove-Item -LiteralPath $temporaryArchive -Force -ErrorAction SilentlyContinue
    Stop-Friendly "本地与服务器快照 ID 不一致。"
}
Write-Host "结构、成员哈希、策略版本和参数哈希全部通过。" -ForegroundColor Green

Write-Step 5 "发布一键生成结果"
Move-Item -LiteralPath $temporaryArchive -Destination $LocalArchive -Force
Extract-SnapshotMember -Archive $LocalArchive -Member "strategy_snapshot.py" -Destination $LocalModule
Extract-SnapshotMember -Archive $LocalArchive -Member "joinquant_native_backtest.py" -Destination $LocalBacktest
Extract-SnapshotMember -Archive $LocalArchive -Member "joinquant_strict_export.py" -Destination $LocalStrictExport
$localResult = [ordered]@{
    status = "success"
    snapshot_id = [string]$result.snapshot_id
    strategy_version = [string]$result.strategy_version
    parameter_version = [string]$result.parameter_version
    code_hash = [string]$result.code_hash
    package_sha256 = $downloadSha256
    archive = $LocalArchive
    joinquant_module = $LocalModule
    joinquant_native_backtest = $LocalBacktest
    joinquant_strict_export = $LocalStrictExport
    source_commit = [string]$result.source_commit
    generated_at = [DateTimeOffset]::UtcNow.ToString("o")
}
Save-LatestResult $localResult
Write-Host "策略快照生成成功！" -ForegroundColor Green
Write-Host "聚宽原生回测：$LocalBacktest"
Write-Host "聚宽严格导出：$LocalStrictExport"
Write-Host "兼容运行时：$LocalModule"
Write-Host "完整证据包：$LocalArchive"
Write-Host ""
Write-Host "脚本没有修改服务器密钥、Token、Webhook、交易配置或数据库。" -ForegroundColor Green
try {
    Start-Process explorer.exe -ArgumentList "/select,`"$LocalStrictExport`""
}
catch {
    Write-Host "提示：无法自动打开文件夹，请按上面的路径找到文件。" -ForegroundColor Yellow
}
exit 0
