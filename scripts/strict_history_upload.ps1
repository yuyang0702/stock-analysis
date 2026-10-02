[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [AllowEmptyString()]
    [string]$PackagePath = "",

    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Exporter = Join-Path $ProjectRoot "joinquant_strict_history_exporter.py"
$ConfigFile = Join-Path $ProjectRoot "cache\strict_history_upload_config.json"
$LatestResult = Join-Path $ProjectRoot "cache\strict_history_upload_latest.json"

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
    param([string]$JsonText)
    $directory = Split-Path -Parent $LatestResult
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $temporary = "$LatestResult.tmp"
    [System.IO.File]::WriteAllText($temporary, $JsonText, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $LatestResult -Force
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    $fileHash = Get-Command Get-FileHash -ErrorAction SilentlyContinue
    if ($null -ne $fileHash) {
        return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
    }

    # Windows PowerShell installations can omit the Microsoft.PowerShell.Utility
    # module when launched with -NoProfile.  Keep validation usable without
    # weakening the hash check by using the .NET implementation as a fallback.
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $algorithm.Dispose()
    }
}

if (-not $PackagePath) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "选择从聚宽下载的 strict 历史数据 ZIP"
    $dialog.Filter = "ZIP 压缩包 (*.zip)|*.zip"
    $dialog.Multiselect = $false
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        Stop-Friendly "你没有选择文件。"
    }
    $PackagePath = $dialog.FileName
}

try {
    $PackagePath = (Resolve-Path -LiteralPath $PackagePath).Path
}
catch {
    Stop-Friendly "找不到你拖入的 ZIP 文件。"
}
if ([System.IO.Path]::GetExtension($PackagePath).ToLowerInvariant() -ne ".zip") {
    Stop-Friendly "请选择聚宽导出的 .zip 文件，不要先解压。"
}
if ((Get-Item -LiteralPath $PackagePath).Length -gt 3000000000) {
    Stop-Friendly "这个 ZIP 超过 3 GB，已按安全上限拒绝。"
}

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        Stop-Friendly "电脑上没有找到 Python。"
    }
    $python = $pythonCommand.Source
}
if (-not (Test-Path -LiteralPath $Exporter -PathType Leaf)) {
    Stop-Friendly "本地导出校验器不存在：$Exporter"
}

Write-Step 1 "检查 ZIP 是否完整"
$verifyRun = Invoke-NativeCapture $python @($Exporter, "verify", $PackagePath)
if ($verifyRun.ExitCode -ne 0) {
    Stop-Friendly "ZIP 校验失败。请重新从聚宽下载，不要修改或解压后重新压缩。`n$($verifyRun.Text)"
}
$verification = Convert-JsonResult $verifyRun.Text "本地校验器"
if ($verification.accepted -ne $true) {
    Stop-Friendly "这个 ZIP 没有通过 strict 数据校验。"
}
$datasetId = [string]$verification.dataset_id
if ($datasetId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$') {
    Stop-Friendly "ZIP 中的数据集名称不符合安全规则。"
}
$packageSha256 = Get-Sha256 -Path $PackagePath
Write-Host "ZIP 完整，数据集：$datasetId" -ForegroundColor Green
Write-Host "文件指纹：$packageSha256"

if ($ValidateOnly) {
    Write-Host ""
    Write-Host "本地检查通过（未上传服务器）。" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path -LiteralPath $ConfigFile -PathType Leaf)) {
    Stop-Friendly "缺少本机上传配置。请联系我完成一次性配置：$ConfigFile"
}
try {
    $uploadConfig = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    Stop-Friendly "本机上传配置无法读取。"
}
$sshTarget = [string]$uploadConfig.ssh_target
$identityFile = [Environment]::ExpandEnvironmentVariables([string]$uploadConfig.identity_file)
$remoteProject = [string]$uploadConfig.remote_project
if ($sshTarget -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$') {
    Stop-Friendly "上传配置中的服务器地址不符合安全规则。"
}
if ($remoteProject -notmatch '^/[A-Za-z0-9._/-]+$' -or $remoteProject.Contains("..")) {
    Stop-Friendly "上传配置中的服务器项目路径不符合安全规则。"
}
if (-not (Test-Path -LiteralPath $identityFile -PathType Leaf)) {
    Stop-Friendly "找不到服务器密钥。密钥没有被修改，请检查配置路径。"
}
$sshCommand = Get-Command ssh -ErrorAction SilentlyContinue
$scpCommand = Get-Command scp -ErrorAction SilentlyContinue
if ($null -eq $sshCommand -or $null -eq $scpCommand) {
    Stop-Friendly "电脑上没有找到 Windows OpenSSH（ssh/scp）。"
}

$remoteInbox = "$remoteProject/cache/backtest/inbox"
$uploadRunId = [Guid]::NewGuid().ToString("N")
# Each invocation owns distinct remote paths.  A retry must never remove a
# content-identical package that an earlier ingest process is still reading.
$remoteStem = "$remoteInbox/$datasetId-$packageSha256-$uploadRunId"
$remoteUploading = "$remoteStem.zip.uploading"
$remoteReady = "$remoteStem.zip"
$sshOptions = @(
    "-i", $identityFile,
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
    "-o", "StrictHostKeyChecking=yes"
)

Write-Step 2 "连接服务器"
$prepare = Invoke-NativeCapture $sshCommand.Source @(
    $sshOptions + @(
        $sshTarget,
        "mkdir -p -- $remoteInbox && rm -f -- $remoteUploading $remoteReady"
    )
)
if ($prepare.ExitCode -ne 0) {
    Stop-Friendly "服务器连接失败。你的 ZIP 仍保留在电脑上。`n$($prepare.Text)"
}
Write-Host "服务器连接成功。" -ForegroundColor Green

Write-Step 3 "上传 ZIP"
$upload = Invoke-NativeCapture $scpCommand.Source @(
    $sshOptions + @($PackagePath, "${sshTarget}:$remoteUploading")
)
if ($upload.ExitCode -ne 0) {
    Stop-Friendly "上传中断。你的本地 ZIP 没有变化，可以直接重试。`n$($upload.Text)"
}
$acceptUpload = Invoke-NativeCapture $sshCommand.Source @(
    $sshOptions + @(
        $sshTarget,
        "echo '$packageSha256  $remoteUploading' | sha256sum -c - && mv -- $remoteUploading $remoteReady"
    )
)
if ($acceptUpload.ExitCode -ne 0) {
    Invoke-NativeCapture $sshCommand.Source @(
        $sshOptions + @($sshTarget, "rm -f -- $remoteUploading $remoteReady")
    ) | Out-Null
    Stop-Friendly "服务器收到的文件指纹不一致，已停止导入。"
}
Write-Host "上传完成，服务器指纹一致。" -ForegroundColor Green

Write-Step 4 "服务器备份并导入"
$ingest = Invoke-NativeCapture $sshCommand.Source @(
    $sshOptions + @(
        $sshTarget,
        "cd $remoteProject && .venv/bin/python strict_history_ingest.py --package $remoteReady"
    )
)
$cleanup = Invoke-NativeCapture $sshCommand.Source @(
    $sshOptions + @($sshTarget, "rm -f -- $remoteUploading $remoteReady")
)
if ($cleanup.ExitCode -ne 0) {
    Write-Host "提醒：服务器临时上传文件未能自动清理，但不会影响历史库。" -ForegroundColor Yellow
}
if ($ingest.Text) {
    Save-LatestResult $ingest.Text
}
$result = Convert-JsonResult $ingest.Text "服务器导入程序"
if ($ingest.ExitCode -ne 0 -or $result.status -ne "success") {
    $reason = [string]$result.error
    switch -Wildcard ($reason) {
        "*COMPLETE_DAILY_FEATURES_REQUIRED*" { $reason = "导出包缺少完整的严格历史特征，请不要导入测试包。" }
        "*PACKAGE_VERSION_CONFLICT*" { $reason = "服务器已有同数据集、同月份但内容不同的包。为防止覆盖，已停止。" }
        "*STRICT_HISTORY_INGEST_ALREADY_RUNNING*" { $reason = "服务器正在导入另一个月包，请稍后再试。" }
        "*LIVE_DATABASE_BUSY*" { $reason = "历史数据库正在被占用，本次没有替换正式库，请稍后再试。" }
        default { if (-not $reason) { $reason = "服务器没有返回具体原因。" } }
    }
    Stop-Friendly "$reason`n本地 ZIP 仍然保留，可以排查后重试。"
}

Write-Step 5 "核对结果"
Write-Host "导入成功！" -ForegroundColor Green
Write-Host "数据集：$($result.dataset_id)"
Write-Host "月份：$($result.month)"
Write-Host "数据集指纹：$($result.dataset_hash)"
Write-Host "数据库完整性：$($result.integrity_check)"
if ($result.idempotent -eq $true) {
    Write-Host "这是重复上传，服务器确认数据一致，没有重复写入。" -ForegroundColor Yellow
}
else {
    Write-Host "服务器已在导入前完成备份，并原子更新历史库。"
}
Write-Host ""
Write-Host "你现在可以关闭这个窗口。" -ForegroundColor Green
exit 0
