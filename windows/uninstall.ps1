<#
.SYNOPSIS
  EV211 Field Server(Windows 네이티브) 서비스·방화벽 규칙을 제거한다. 데이터는 기본 보존.

.DESCRIPTION
  기본 동작: 서비스 3개 중지·삭제, EV211 방화벽 규칙 삭제, 실행 파일(bin·venv·app·services) 삭제.
  보존: config\ev211.env(비밀), data\(DB·Caddy CA), recordings\, logs\, backups\ — 재설치 시 그대로 재사용된다.
  -PurgeData: 위 보존 항목까지 <Root> 전체를 삭제한다(복구 불가 — 먼저 ev211ctl backup 권장).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\windows\uninstall.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File .\windows\uninstall.ps1 -PurgeData -Yes
#>
[CmdletBinding()]
param(
    [string]$Root = '',
    [switch]$PurgeData,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ev211-common.ps1')

if (-not $Root) {
    $installedRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    if (Test-Path -LiteralPath (Join-Path $installedRoot 'config\ev211.env')) { $Root = $installedRoot }
    else { $Root = Get-EV211DefaultRoot }
}
$P = Get-EV211Paths -Root $Root
Assert-EV211Administrator

if ($PurgeData -and -not $Yes) {
    Write-Host "경고: -PurgeData 는 $($P.Root) 전체(비밀번호·DB·녹음·백업·Caddy CA)를 삭제합니다." -ForegroundColor Yellow
    $answer = Read-Host '정말 삭제하려면 PURGE 를 입력하세요'
    if ($answer -ne 'PURGE') { Write-Host '취소했습니다.'; exit 1 }
}

Write-EV211Step '서비스 중지·삭제'
Stop-EV211Services
Uninstall-EV211Services -Paths $P

Write-EV211Step '방화벽 규칙 삭제'
Remove-EV211FirewallRules

# 이 스크립트가 설치본(<Root>\app\windows)에서 실행 중일 수 있으므로, 삭제는 스크립트 파일이
# 이미 메모리에 로드된 지금 수행한다(PowerShell 은 실행 중 .ps1 파일을 잠그지 않는다).
if ($PurgeData) {
    Write-EV211Step "전체 삭제: $($P.Root)"
    if (Test-Path -LiteralPath $P.Root) { Remove-Item -LiteralPath $P.Root -Recurse -Force }
    Write-Host '모든 데이터를 삭제했습니다.'
}
else {
    Write-EV211Step '실행 파일 삭제(데이터 보존)'
    foreach ($dir in @($P.Services, $P.Bin, $P.Venv, $P.App)) {
        if (Test-Path -LiteralPath $dir) {
            Remove-Item -LiteralPath $dir -Recurse -Force
            Write-Host "삭제: $dir"
        }
    }
    $shim = Join-Path $P.Root 'ev211ctl.cmd'
    if (Test-Path -LiteralPath $shim) { Remove-Item -LiteralPath $shim -Force }
    Write-Host ''
    Write-Host "보존: $($P.Config), $($P.Data), $($P.Recordings), $($P.Logs), $($P.Backups)"
    Write-Host '같은 Root 로 install.ps1 을 다시 실행하면 기존 비밀번호·DB·인증서를 그대로 사용합니다.'
}
Write-Host '제거가 완료되었습니다.' -ForegroundColor Green
