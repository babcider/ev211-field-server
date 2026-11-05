<#
.SYNOPSIS
  EV211 Field Server 를 도커 없이 Windows 서비스 3개(LiveKit·field-api·Caddy)로 설치한다(PoC).

.DESCRIPTION
  scripts/install.sh(Ubuntu) 의 Windows 네이티브 대응판. 관리자 PowerShell 에서 실행한다.
  - <Root>\{bin,app,venv,config,services,data,recordings,logs,backups} 생성, 관리자·SYSTEM 전용 ACL
  - 서버 LAN IP 감지, LiveKit 키·송신/관리자 비밀번호 생성 → config\ev211.env (재실행 시 기존 값 유지)
  - livekit.yaml 렌더링(livekit-entrypoint.sh 포팅), Python 3.12 venv + 해시 고정 의존성 설치
  - WinSW 로 서비스 등록(자동 시작·의존성·실패 시 재시작), 방화벽(개인 프로필) 규칙, 기동 후 진단

.PARAMETER DownloadDir
  고정 버전 원본 파일이 있는 폴더(livekit_1.13.3_windows_amd64.zip, caddy_2.11.4_windows_amd64.zip,
  WinSW-x64.exe, lame\lame.exe). 기본값은 이 스크립트 옆의 downloads\.

.PARAMETER Download
  LiveKit·Caddy·WinSW 를 GitHub 릴리스에서 DownloadDir 로 내려받는다(체크섬 검증은 항상 수행). lame 은 제외.

.PARAMETER LameDir
  lame.exe(및 동봉 DLL)가 있는 폴더. 기본값 <DownloadDir>\lame.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\windows\install.ps1 -Download
#>
[CmdletBinding()]
param(
    [string]$Root = '',
    [string]$DownloadDir = '',
    [switch]$Download,
    [string]$LameDir = '',
    [string]$PythonExe = '',
    [string]$NodeIp = '',
    [int]$UdpStart = 50000,
    [int]$UdpEnd = 60000,
    [switch]$RecreateVenv,
    [switch]$SkipPortCheck,
    [switch]$SkipFirewall,
    [switch]$ConfigurePower
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'ev211-common.ps1')

$SourceDir = Split-Path -Parent $PSScriptRoot
if (-not $Root) { $Root = Get-EV211DefaultRoot }
if (-not $DownloadDir) { $DownloadDir = Join-Path $PSScriptRoot 'downloads' }
if (-not $LameDir) { $LameDir = Join-Path $DownloadDir 'lame' }
$P = Get-EV211Paths -Root $Root

# ---- 0. 전제 조건 -------------------------------------------------------------------------------
Write-EV211Step '전제 조건 확인'
Assert-EV211Administrator
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Windows 에서만 실행할 수 있습니다.' }
if (-not [Environment]::Is64BitOperatingSystem) { throw '64비트(x64) Windows 만 지원합니다.' }
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64') {
    throw "x64(AMD64) CPU 만 지원합니다(현재: $env:PROCESSOR_ARCHITECTURE). ARM64 는 미검증."
}
foreach ($sourcePath in @('api\app', 'api\requirements.lock', 'web', 'livekit.yaml', 'windows\Caddyfile.windows', 'windows\ev211-common.ps1', 'windows\ev211ctl.ps1')) {
    if (-not (Test-Path -LiteralPath (Join-Path $SourceDir $sourcePath))) { throw "저장소 파일이 없습니다: $sourcePath (저장소 루트의 windows\install.ps1 을 실행하세요)" }
}
Get-EV211Tar | Out-Null
if (-not (Test-Path -LiteralPath "$env:SystemRoot\System32\curl.exe")) { throw 'curl.exe 가 없습니다(Windows 10 1803 이상 필요).' }
if ($UdpStart -lt 1024 -or $UdpEnd -gt 65535 -or $UdpStart -ge $UdpEnd) { throw "UDP 범위가 올바르지 않습니다: $UdpStart-$UdpEnd" }
if ($NodeIp -and -not (Test-EV211IPv4 $NodeIp)) { throw "-NodeIp 가 IPv4 주소가 아닙니다: $NodeIp" }

# ---- 1. 바이너리 확보·검증(변경 전에 모두 검증) ------------------------------------------------
Write-EV211Step '고정 버전 바이너리 확인'
if (-not (Test-Path -LiteralPath $DownloadDir)) { New-Item -ItemType Directory -Path $DownloadDir | Out-Null }
if ($Download) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    foreach ($name in @('LiveKit', 'Caddy', 'WinSW')) {
        $pin = $script:EV211Pins[$name]
        $target = Join-Path $DownloadDir $pin.File
        if (-not (Test-Path -LiteralPath $target)) {
            Write-Host "다운로드: $($pin.Url)"
            Invoke-WebRequest -Uri $pin.Url -OutFile $target -UseBasicParsing
        }
    }
}
foreach ($name in @('LiveKit', 'Caddy', 'WinSW')) {
    $pin = $script:EV211Pins[$name]
    $file = Join-Path $DownloadDir $pin.File
    if (-not (Test-Path -LiteralPath $file)) {
        throw "$($pin.File) 가 없습니다. $($pin.Url) 에서 받아 $DownloadDir 에 두거나 -Download 를 붙이세요."
    }
    $actual = (Get-FileHash -LiteralPath $file -Algorithm $pin.Algorithm).Hash.ToLowerInvariant()
    if ($actual -ne $pin.Hash) {
        throw "$($pin.File) 체크섬 불일치($($pin.Algorithm)). 기대 $($pin.Hash) / 실제 $actual — 파일을 삭제하고 다시 받으세요."
    }
    Write-Host "검증 통과: $($pin.File) ($name $($pin.Version), $($pin.Algorithm))"
}
$lameExe = Join-Path $LameDir 'lame.exe'
if (-not (Test-Path -LiteralPath $lameExe)) {
    throw "lame.exe 가 없습니다: $lameExe — README 의 'lame 준비' 절을 따르세요(녹음 MP3 인코더)."
}
$lameHash = (Get-FileHash -LiteralPath $lameExe -Algorithm SHA256).Hash.ToLowerInvariant()
Write-EV211Warn "lame.exe 는 공식 서명·체크섬이 없어 고정 해시로 검증하지 않습니다. SHA256=$lameHash (배포처 값과 직접 대조하세요)"

# ---- 2. Python 3.12 x64 확인 ------------------------------------------------------------------
Write-EV211Step 'Python 3.12 (x64) 확인'
if (-not $PythonExe) {
    $pyLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $resolved = & $pyLauncher.Source -3.12 -c 'import sys; print(sys.executable)'
        if ($LASTEXITCODE -eq 0 -and $resolved) { $PythonExe = ([string]$resolved).Trim() }
    }
}
if (-not $PythonExe) {
    $pythonCmd = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($pythonCmd) { $PythonExe = $pythonCmd.Source }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) {
    throw 'Python 3.12 x64 를 찾지 못했습니다. python.org 설치 관리자로 "Install for all users" 설치 후 다시 실행하거나 -PythonExe 로 지정하세요.'
}
if ($PythonExe -match '\\WindowsApps\\') {
    throw "Microsoft Store 판 Python($PythonExe)은 SYSTEM 서비스에서 실행할 수 없습니다. python.org 설치판을 쓰세요."
}
# ⚠ PowerShell 5.1 은 네이티브 인자 속 큰따옴표를 이스케이프하지 않으므로 -c 코드에는 작은따옴표만 쓴다.
$pyInfo = & $PythonExe -c 'import struct,sys; print(sys.version_info[0], sys.version_info[1], struct.calcsize(''P'')*8)'
Assert-EV211ExitCode 'Python 버전 확인'
if (([string]$pyInfo).Trim() -ne '3 12 64') {
    throw "Python 3.12 64비트가 필요합니다(requirements.lock 이 cp312 휠 기준). 현재: $pyInfo ($PythonExe)"
}
Write-Host "Python: $PythonExe"
if ($PythonExe -match '^[A-Za-z]:\\Users\\') {
    Write-EV211Warn '사용자 프로필 아래 Python 입니다. 해당 사용자를 삭제하면 서비스가 멈춥니다. "Install for all users" 를 권장합니다.'
}

# ---- 3. 디렉터리·ACL ---------------------------------------------------------------------------
Write-EV211Step "설치 디렉터리 준비: $($P.Root)"
if (-not (Test-Path -LiteralPath $P.Root)) { New-Item -ItemType Directory -Path $P.Root | Out-Null }
# 루트에 먼저 제한 ACL 을 걸고 하위는 상속받게 한다(DB·녹음·CA 개인키·비밀 모두 관리자·SYSTEM 전용).
Set-EV211RestrictedAcl -Path $P.Root
foreach ($dir in @($P.Bin, $P.LameDir, $P.App, $P.Config, $P.Services, $P.Data, $P.CaddyData, $P.CaddyConfig, $P.Recordings, $P.Logs, $P.Backups)) {
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
}

# 재실행이면 실행 중 서비스가 바이너리·venv 를 잠그므로 먼저 멈춘다.
Stop-EV211Services

if (-not $SkipPortCheck) {
    $conflicts = @(Get-EV211PortConflicts -Ports @(80, 8443, 8880, 7880, 7881, 8000))
    if ($conflicts.Count -gt 0) {
        throw ("다음 포트를 이미 다른 프로그램이 사용 중입니다:`n  " + ($conflicts -join "`n  ") + "`n해당 프로그램을 중지하거나 -SkipPortCheck 로 건너뛰세요.")
    }
}

# ---- 4. 바이너리 배치 ---------------------------------------------------------------------------
Write-EV211Step '바이너리 배치'
$extractRoot = Join-Path $P.Root ('.extract-' + [Guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $extractRoot | Out-Null
    foreach ($item in @(@{ Pin = 'LiveKit'; Exe = 'livekit-server.exe'; Target = $P.LiveKitExe }, @{ Pin = 'Caddy'; Exe = 'caddy.exe'; Target = $P.CaddyExe })) {
        $zip = Join-Path $DownloadDir $script:EV211Pins[$item.Pin].File
        $dest = Join-Path $extractRoot $item.Pin
        Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
        $found = @(Get-ChildItem -LiteralPath $dest -Recurse -Filter $item.Exe -File)
        if ($found.Count -ne 1) { throw "$zip 안에서 $($item.Exe) 를 찾지 못했습니다." }
        Copy-Item -LiteralPath $found[0].FullName -Destination $item.Target -Force
        Write-Host "배치: $($item.Target)"
    }
}
finally {
    if (Test-Path -LiteralPath $extractRoot) { Remove-Item -LiteralPath $extractRoot -Recurse -Force }
}
Copy-Item -LiteralPath (Join-Path $DownloadDir $script:EV211Pins.WinSW.File) -Destination $P.WinSWExe -Force
# -LameDir 가 설치본(bin\lame) 자체를 가리키면 지우고 복사하는 대신 그대로 둔다.
if ([System.IO.Path]::GetFullPath($LameDir).TrimEnd('\') -ne $P.LameDir.TrimEnd('\')) {
    Get-ChildItem -LiteralPath $P.LameDir -Force | Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $LameDir -Force | Copy-Item -Destination $P.LameDir -Recurse -Force
}
$manifest = @(
    "# 설치된 외부 바이너리 기록 — install.ps1 생성 $(Get-Date -Format s)",
    "livekit-server $($script:EV211Pins.LiveKit.Version) $($script:EV211Pins.LiveKit.Algorithm)=$($script:EV211Pins.LiveKit.Hash)",
    "caddy $($script:EV211Pins.Caddy.Version) $($script:EV211Pins.Caddy.Algorithm)=$($script:EV211Pins.Caddy.Hash)",
    "winsw $($script:EV211Pins.WinSW.Version) $($script:EV211Pins.WinSW.Algorithm)=$($script:EV211Pins.WinSW.Hash)",
    "lame.exe (고정 해시 없음) SHA256=$lameHash",
    "python $PythonExe"
)
Write-EV211Utf8File -Path $P.BinaryManifest -Content (($manifest -join "`r`n") + "`r`n")

# ---- 5. 앱 소스 복사(install.sh 의 /opt 복사와 동일 역할) ----------------------------------------
Write-EV211Step '앱 파일 복사'
foreach ($stale in @((Join-Path $P.ApiDir 'app'), $P.WebDir, $P.WindowsDir)) {
    if (Test-Path -LiteralPath $stale) { Remove-Item -LiteralPath $stale -Recurse -Force }
}
New-Item -ItemType Directory -Path $P.ApiDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $SourceDir 'api\app') -Destination (Join-Path $P.ApiDir 'app') -Recurse -Force
Get-ChildItem -LiteralPath (Join-Path $P.ApiDir 'app') -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
foreach ($file in @('requirements.lock', 'requirements.txt')) {
    Copy-Item -LiteralPath (Join-Path $SourceDir "api\$file") -Destination (Join-Path $P.ApiDir $file) -Force
}
Copy-Item -LiteralPath (Join-Path $SourceDir 'web') -Destination $P.WebDir -Recurse -Force
Copy-Item -LiteralPath (Join-Path $SourceDir 'livekit.yaml') -Destination $P.LiveKitTemplate -Force
Copy-Item -LiteralPath (Join-Path $SourceDir 'windows\Caddyfile.windows') -Destination $P.Caddyfile -Force
New-Item -ItemType Directory -Path $P.WindowsDir -Force | Out-Null
foreach ($file in @('ev211-common.ps1', 'ev211ctl.ps1', 'uninstall.ps1', 'README.md')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $P.WindowsDir $file) -Force
}
# 관리자 셸에서 어디서든 부르기 쉽게 루트에 .cmd 래퍼를 둔다(실행 정책 우회는 이 파일 범위에 한정).
$cmdShim = "@echo off`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File ""%~dp0app\windows\ev211ctl.ps1"" %*`r`n"
Write-EV211Utf8File -Path (Join-Path $P.Root 'ev211ctl.cmd') -Content $cmdShim

# ---- 6. 설정 파일(ev211.env) — 기존 비밀은 절대 재생성하지 않는다 ---------------------------------
Write-EV211Step "설정 파일: $($P.EnvFile)"
if (-not (Test-Path -LiteralPath $P.EnvFile)) {
    if (-not $NodeIp) { $NodeIp = Get-EV211LanIp }
    if (-not $NodeIp) { throw '서버 LAN IP를 감지하지 못했습니다. -NodeIp 192.168.x.x 로 지정하세요.' }
    $apiKey = 'field_' + (New-EV211RandomHex 6)
    $apiSecret = New-EV211RandomHex 32
    $lines = @(
        '# EV211 Field Server(Windows 네이티브) 설정 — install.ps1 생성. 관리자·SYSTEM 전용 파일, 커밋·공유 금지',
        '# 값을 바꾼 뒤에는 ev211ctl restart 로 반영한다(livekit.yaml·서비스 XML 을 다시 렌더링).',
        "LIVEKIT_API_KEY=$apiKey",
        "LIVEKIT_API_SECRET=$apiSecret",
        "LIVEKIT_KEYS=""${apiKey}: $apiSecret""",
        "FIELD_NODE_IP=$NodeIp",
        "FIELD_WS_URL=ws://${NodeIp}:7880",
        "FIELD_SEND_PASSWORD=send_$(New-EV211RandomHex 12)",
        "FIELD_ADMIN_PASSWORD=admin_$(New-EV211RandomHex 12)",
        "FIELD_UDP_START=$UdpStart",
        "FIELD_UDP_END=$UdpEnd",
        'MAX_CHANNELS=15',
        '# FIELD_DB_PATH·FIELD_RECORDINGS_PATH 는 서비스 XML 이 <Root>\data·<Root>\recordings 로 고정 주입한다.',
        '# 모드 C AI 통역(선택): OPENAI_API_KEY='
    )
    # 먼저 빈 파일을 만들어 ACL 을 건 뒤 내용을 쓴다(비밀이 잠깐이라도 넓은 권한으로 남지 않게).
    Write-EV211Utf8File -Path $P.EnvFile -Content ''
    Set-EV211RestrictedAcl -Path $P.EnvFile
    Write-EV211Utf8File -Path $P.EnvFile -Content (($lines -join "`r`n") + "`r`n")
    Write-Host "새 비밀을 생성했습니다(서버 IP $NodeIp)."
}
else {
    Write-Host "기존 설정을 유지합니다: $($P.EnvFile)"
    if ($NodeIp) {
        Set-EV211EnvValue -Path $P.EnvFile -Key 'FIELD_NODE_IP' -Value $NodeIp
        Set-EV211EnvValue -Path $P.EnvFile -Key 'FIELD_WS_URL' -Value "ws://${NodeIp}:7880"
        Write-Host "-NodeIp 지정 → FIELD_NODE_IP=$NodeIp, FIELD_WS_URL=ws://${NodeIp}:7880 로 갱신"
    }
    Set-EV211RestrictedAcl -Path $P.EnvFile
}
$envMap = Read-EV211EnvFile -Path $P.EnvFile
foreach ($key in @('LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET', 'FIELD_NODE_IP', 'FIELD_SEND_PASSWORD', 'FIELD_ADMIN_PASSWORD')) {
    Get-EV211EnvValue $envMap $key -Required | Out-Null
}

# ---- 7. Python venv + 해시 고정 의존성 ----------------------------------------------------------
Write-EV211Step 'Python 가상환경·의존성 설치'
if ($RecreateVenv -and (Test-Path -LiteralPath $P.Venv)) { Remove-Item -LiteralPath $P.Venv -Recurse -Force }
if (-not (Test-Path -LiteralPath $P.VenvPython)) {
    & $PythonExe -m venv $P.Venv
    Assert-EV211ExitCode 'venv 생성'
}
# Dockerfile 과 같은 명령. requirements.lock 은 uv --universal 로 생성돼 cp312 win_amd64 휠 해시를 포함한다
# (2026-11-05 확인: livekit 1.1.13 은 py3-none-win_amd64 휠 제공, uvloop 는 win32 마커로 제외).
& $P.VenvPython -m pip install --disable-pip-version-check --no-cache-dir --require-hashes -r (Join-Path $P.ApiDir 'requirements.lock')
Assert-EV211ExitCode 'pip install --require-hashes'
Push-Location $P.ApiDir
try {
    & $P.VenvPython -c 'import livekit.rtc, app.main; print(''import ok'')'
    Assert-EV211ExitCode 'field-api 임포트 확인(livekit 네이티브 FFI 포함)'
}
finally { Pop-Location }

# ---- 8. livekit.yaml·서비스 XML 렌더링 + 서비스 등록 ---------------------------------------------
Write-EV211Step 'LiveKit 설정 렌더링·서비스 등록(WinSW)'
Update-EV211RuntimeConfig -Paths $P | Out-Null
Install-EV211Services -Paths $P

# ---- 9. 방화벽 -----------------------------------------------------------------------------------
if (-not $SkipFirewall) {
    Write-EV211Step 'Windows 방화벽 규칙(개인 프로필 전용)'
    Set-EV211FirewallRules -UdpStart ([int](Get-EV211EnvValue $envMap 'FIELD_UDP_START' "$UdpStart")) -UdpEnd ([int](Get-EV211EnvValue $envMap 'FIELD_UDP_END' "$UdpEnd"))
}

# ---- 10. 전원(선택) ------------------------------------------------------------------------------
if ($ConfigurePower) {
    Write-EV211Step '전원 설정: AC 전원에서 절전·최대 절전·디스크 끄기 해제'
    & powercfg.exe /change standby-timeout-ac 0
    & powercfg.exe /change hibernate-timeout-ac 0
    & powercfg.exe /change disk-timeout-ac 0
}

# ---- 11. 기동·진단 -------------------------------------------------------------------------------
Write-EV211Step '서비스 시작'
Start-EV211Services
& (Join-Path $P.WindowsDir 'ev211ctl.ps1') -Root $P.Root doctor
$doctorCode = $LASTEXITCODE

Write-Host ''
Write-Host '설치가 완료되었습니다.' -ForegroundColor Green
Write-Host "접속 정보: $($P.Root)\ev211ctl.cmd credentials"
Write-Host "상태 점검: $($P.Root)\ev211ctl.cmd doctor"
Write-Host 'DHCP 예약(고정 IP)·네트워크 프로필(개인)·절전 해제는 windows\README.md 를 따르세요.'
if ($doctorCode -ne 0) {
    Write-EV211Warn '진단에 실패 항목이 있습니다. 위 출력과 logs\ 폴더를 확인하세요.'
    exit 1
}
