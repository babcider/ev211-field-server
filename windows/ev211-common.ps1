# EV211 Windows 네이티브 키트 공통 함수 — install.ps1·ev211ctl.ps1·uninstall.ps1 이 dot-source 한다
# Windows PowerShell 5.1(Windows 10/11 기본) 호환 문법만 사용한다(??, ?., 삼항, && 금지).
# ⚠ 이 파일과 다른 .ps1 은 UTF-8 **BOM 포함**으로 저장해야 한다. BOM 이 없으면 PowerShell 5.1 이
#   시스템 코드페이지(CP949)로 읽어 한글 문자열이 깨지고 구문 오류가 날 수 있다.

Set-StrictMode -Version 2.0

# ---- 고정 상수 ------------------------------------------------------------------------------

# 서비스 ID(=WinSW 실행 파일 이름). 기동 순서 = 배열 순서, 중지 순서 = 역순.
$script:EV211ServiceIds = @('ev211-livekit', 'ev211-api', 'ev211-caddy')
$script:EV211FirewallGroup = 'EV211 Field Server'
# LiveKit 웹훅 — 도커 host 오버레이와 동일하게 Caddy 루프백(8880) /api 프록시를 경유한다.
# field-api(127.0.0.1:8000)로 직접 보내지 않는 이유: Caddy 가 XFF 를 127.0.0.1 로 고정하는 경로를 유지.
$script:EV211WebhookUrl = 'http://localhost:8880/api/livekit/webhook'

# 외부 바이너리 고정 버전·체크섬. 값의 출처는 README.md 의 "바이너리 고정 버전" 표 참고.
#  - LiveKit: 릴리스의 checksums.txt (SHA256)
#  - Caddy  : 릴리스의 caddy_<ver>_checksums.txt (SHA512)
#  - WinSW  : 공식 체크섬 파일이 없어 2026-11-05 GitHub 릴리스 자산을 직접 해시(SHA256)
# 버전을 올릴 때는 docker-compose.yml 의 이미지 버전과 함께 갱신한다.
$script:EV211Pins = @{
    LiveKit = @{
        Version   = '1.13.3'
        File      = 'livekit_1.13.3_windows_amd64.zip'
        Url       = 'https://github.com/livekit/livekit/releases/download/v1.13.3/livekit_1.13.3_windows_amd64.zip'
        Algorithm = 'SHA256'
        Hash      = 'd29786e63a11de390fb8051191a30520cfb687d8f6cb5063b6ef0f748f029727'
    }
    Caddy   = @{
        Version   = '2.11.4'
        File      = 'caddy_2.11.4_windows_amd64.zip'
        Url       = 'https://github.com/caddyserver/caddy/releases/download/v2.11.4/caddy_2.11.4_windows_amd64.zip'
        Algorithm = 'SHA512'
        Hash      = 'cd5ccfd86a4b40732cf715890d0dca5bf3f63adefec5a7914de85adf240c60ce7e5d2791631b88ef9758e46b23bb1730e020b9c5d696889740b284ffd4788e35'
    }
    WinSW   = @{
        Version   = '2.12.0'
        File      = 'WinSW-x64.exe'
        Url       = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
        Algorithm = 'SHA256'
        Hash      = '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'
    }
}

# ---- 경로 ------------------------------------------------------------------------------------

function Get-EV211DefaultRoot {
    if ($env:EV211_ROOT) { return $env:EV211_ROOT }
    return (Join-Path $env:ProgramData 'EV211Field')
}

function Get-EV211Paths {
    param([Parameter(Mandatory = $true)][string]$Root)
    $root = [System.IO.Path]::GetFullPath($Root)
    $app = Join-Path $root 'app'
    $data = Join-Path $root 'data'
    $config = Join-Path $root 'config'
    $bin = Join-Path $root 'bin'
    $venv = Join-Path $root 'venv'
    return [pscustomobject]@{
        Root            = $root
        Bin             = $bin
        LameDir         = (Join-Path $bin 'lame')
        LiveKitExe      = (Join-Path $bin 'livekit-server.exe')
        CaddyExe        = (Join-Path $bin 'caddy.exe')
        WinSWExe        = (Join-Path $bin 'WinSW-x64.exe')
        App             = $app
        ApiDir          = (Join-Path $app 'api')
        WebDir          = (Join-Path $app 'web')
        WindowsDir      = (Join-Path $app 'windows')
        LiveKitTemplate = (Join-Path $app 'livekit.template.yaml')
        Caddyfile       = (Join-Path $app 'Caddyfile')
        Venv            = $venv
        VenvPython      = (Join-Path (Join-Path $venv 'Scripts') 'python.exe')
        Config          = $config
        EnvFile         = (Join-Path $config 'ev211.env')
        LiveKitConfig   = (Join-Path $config 'livekit.yaml')
        BinaryManifest  = (Join-Path $config 'binaries.txt')
        Services        = (Join-Path $root 'services')
        Data            = $data
        DbPath          = (Join-Path $data 'field.db')
        CaddyData       = (Join-Path $data 'caddy-data')
        CaddyConfig     = (Join-Path $data 'caddy-config')
        Recordings      = (Join-Path $root 'recordings')
        Logs            = (Join-Path $root 'logs')
        Backups         = (Join-Path $root 'backups')
    }
}

# ---- 공통 유틸 -------------------------------------------------------------------------------

function Assert-EV211Administrator {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '관리자 권한이 필요합니다. "관리자 권한으로 실행" 한 PowerShell 에서 다시 실행하세요.'
    }
}

function Write-EV211Step {
    param([string]$Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-EV211Warn {
    param([string]$Message)
    Write-Host "경고: $Message" -ForegroundColor Yellow
}

# UTF-8(BOM 없음)으로 쓴다. PowerShell 5.1 의 Set-Content -Encoding UTF8 은 BOM 을 붙여
# python-dotenv 가 첫 키 이름을 '﻿LIVEKIT_API_KEY' 로 읽는 문제가 생긴다.
function Write-EV211Utf8File {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

# 네이티브 명령 실행 후 종료 코드를 검사한다($ErrorActionPreference 는 네이티브 실패를 잡지 못함).
function Assert-EV211ExitCode {
    param([string]$What)
    if ($LASTEXITCODE -ne 0) { throw "$What 실패(종료 코드 $LASTEXITCODE)" }
}

# Administrators(S-1-5-32-544) + SYSTEM(S-1-5-18) 만 전체 권한, 상속 차단.
# 계정 이름은 OS 언어마다 다르므로(한국어 Windows 는 '관리자'가 아닌 그룹명이 현지화됨) SID 로 지정한다.
function Set-EV211RestrictedAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer) {
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    }
    else {
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $inherit = [System.Security.AccessControl.InheritanceFlags]::None
    }
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sidValue in @('S-1-5-32-544', 'S-1-5-18')) {
        $sid = New-Object System.Security.Principal.SecurityIdentifier($sidValue)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $sid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            $inherit,
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function New-EV211RandomHex {
    param([Parameter(Mandatory = $true)][int]$Bytes)
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return (($buffer | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Test-EV211IPv4 {
    param([string]$Value)
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse([string]$Value, [ref]$parsed)) { return $false }
    return ($parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and $Value -match '^\d{1,3}(\.\d{1,3}){3}$')
}

# ---- env 파일 --------------------------------------------------------------------------------

# KEY=VALUE 형식(# 주석, 양끝 따옴표 제거). python-dotenv·bash `. ./.env` 와 호환되는 단순 형식만 다룬다.
function Read-EV211EnvFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    $result = [ordered]@{}
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $text = $line.Trim()
        if ($text -eq '' -or $text.StartsWith('#')) { continue }
        $index = $text.IndexOf('=')
        if ($index -lt 1) { continue }
        $key = $text.Substring(0, $index).Trim()
        if ($key.StartsWith('export ')) { $key = $key.Substring(7).Trim() }
        $value = $text.Substring($index + 1).Trim()
        if ($value.Length -ge 2) {
            $first = $value.Substring(0, 1)
            $last = $value.Substring($value.Length - 1, 1)
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $result[$key] = $value
    }
    return $result
}

function Get-EV211EnvValue {
    param($EnvMap, [string]$Key, [string]$Default = $null, [switch]$Required)
    if ($EnvMap.Contains($Key) -and [string]$EnvMap[$Key] -ne '') { return [string]$EnvMap[$Key] }
    if ($Required) { throw "ev211.env 에 $Key 가 없습니다." }
    return $Default
}

# 키가 있으면 값만 교체, 없으면 끝에 추가한다(다른 줄·주석 보존). 파일 ACL 은 유지된다.
function Set-EV211EnvValue {
    param([Parameter(Mandatory = $true)][string]$Path, [string]$Key, [string]$Value)
    $lines = New-Object System.Collections.Generic.List[string]
    $found = $false
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*='
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match $pattern) {
            if (-not $found) { $lines.Add("$Key=$Value") }
            $found = $true
        }
        else {
            $lines.Add($line)
        }
    }
    if (-not $found) { $lines.Add("$Key=$Value") }
    Write-EV211Utf8File -Path $Path -Content (($lines -join "`r`n") + "`r`n")
}

# ---- LAN IP 감지 (scripts/detect-ip.sh 의 Linux 분기 `ip route get 1.1.1.1` 포팅) -----------------

function Get-EV211LanIp {
    $virtualPattern = 'vEthernet|Hyper-V|VirtualBox|VMware|Loopback|Tailscale|ZeroTier|WSL|Npcap|Bluetooth'

    # 1) 기본 경로의 소스 주소(인터넷이 없어도 기본 게이트웨이 경로만 있으면 동작).
    try {
        $candidates = @(Find-NetRoute -RemoteIPAddress '1.1.1.1' -ErrorAction Stop |
            Where-Object { $_.PSObject.Properties['IPAddress'] -and (Test-EV211IPv4 $_.IPAddress) -and $_.IPAddress -notmatch '^169\.254\.' })
        if ($candidates.Count -gt 0) { return [string]$candidates[0].IPAddress }
    }
    catch { Write-Verbose "무시(폴백 진행): $_" }

    # 2) 기본 게이트웨이가 있는 활성 어댑터의 IPv4.
    try {
        $configs = @(Get-NetIPConfiguration -ErrorAction Stop |
            Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter -and $_.NetAdapter.Status -eq 'Up' -and $_.InterfaceAlias -notmatch $virtualPattern })
        foreach ($config in $configs) {
            foreach ($address in @($config.IPv4Address)) {
                if ($address -and (Test-EV211IPv4 $address.IPAddress) -and $address.IPAddress -notmatch '^169\.254\.') {
                    return [string]$address.IPAddress
                }
            }
        }
    }
    catch { Write-Verbose "무시(폴백 진행): $_" }

    # 3) 게이트웨이 없는 폐쇄망(공유기 없이 스위치만) — 가상 어댑터를 제외한 첫 사설 IPv4.
    try {
        $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.InterfaceAlias -notmatch $virtualPattern } |
            Sort-Object -Property InterfaceMetric)
        if ($addresses.Count -gt 0) { return [string]$addresses[0].IPAddress }
    }
    catch { Write-Verbose "무시(폴백 진행): $_" }

    return $null
}

# ---- LiveKit 설정 렌더링 (scripts/livekit-entrypoint.sh 포팅) -----------------------------------

function ConvertTo-EV211YamlString {
    param([string]$Value)
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

# livekit.yaml(템플릿)의 ${LIVEKIT_API_KEY}·${LIVEKIT_WEBHOOK_URL} 를 치환하고, UDP 범위를 ev211.env 값으로
# 고정하며, keys 를 렌더링본에만 추가한다. 도커판은 keys 를 LIVEKIT_KEYS 환경변수로 넣지만, 네이티브에서는
# 서비스 XML 에 비밀을 두지 않기 위해 관리자·SYSTEM 전용 ACL 의 config\livekit.yaml 에만 기록한다.
# (livekit-server --key-file 은 Go 의 POSIX 권한 검사(0600) 때문에 Windows 에서 쓸 수 없다.)
function Write-EV211LiveKitConfig {
    param([Parameter(Mandatory = $true)]$Paths, [Parameter(Mandatory = $true)]$EnvMap)
    $apiKey = Get-EV211EnvValue $EnvMap 'LIVEKIT_API_KEY' -Required
    $apiSecret = Get-EV211EnvValue $EnvMap 'LIVEKIT_API_SECRET' -Required
    $udpStart = [int](Get-EV211EnvValue $EnvMap 'FIELD_UDP_START' '50000')
    $udpEnd = [int](Get-EV211EnvValue $EnvMap 'FIELD_UDP_END' '60000')
    if ($udpStart -lt 1024 -or $udpEnd -gt 65535 -or $udpStart -ge $udpEnd) {
        throw "FIELD_UDP_START/END 범위가 올바르지 않습니다: $udpStart-$udpEnd"
    }
    if ($apiKey -notmatch '^[A-Za-z0-9_\-]+$') {
        throw 'LIVEKIT_API_KEY 는 영문·숫자·_·- 만 허용합니다(템플릿 치환 안전성).'
    }

    $template = [System.IO.File]::ReadAllText($Paths.LiveKitTemplate)
    foreach ($token in @('${LIVEKIT_API_KEY}', '${LIVEKIT_WEBHOOK_URL}')) {
        if (-not $template.Contains($token)) { throw "livekit 템플릿에 $token 자리표시자가 없습니다." }
    }
    $rendered = $template.Replace('${LIVEKIT_API_KEY}', $apiKey).Replace('${LIVEKIT_WEBHOOK_URL}', $script:EV211WebhookUrl)

    $startPattern = '(?m)^(\s*port_range_start:)\s*\d+'
    $endPattern = '(?m)^(\s*port_range_end:)\s*\d+'
    if (-not ([regex]::IsMatch($rendered, $startPattern) -and [regex]::IsMatch($rendered, $endPattern))) {
        throw 'livekit 템플릿에 rtc.port_range_start/end 가 없습니다.'
    }
    $rendered = [regex]::Replace($rendered, $startPattern, ('$1 ' + $udpStart))
    $rendered = [regex]::Replace($rendered, $endPattern, ('$1 ' + $udpEnd))
    if ($rendered -match '(?m)^keys:') { throw 'livekit 템플릿에 keys 가 이미 있습니다(평문 키 커밋 금지 원칙 위반).' }

    $rendered = $rendered.TrimEnd() + "`n`n" +
        "# ---- 아래는 ev211.env 에서 렌더링(windows/ev211-common.ps1). 관리자·SYSTEM 전용 파일 ----`n" +
        "keys:`n" +
        '  ' + (ConvertTo-EV211YamlString $apiKey) + ': ' + (ConvertTo-EV211YamlString $apiSecret) + "`n"
    Write-EV211Utf8File -Path $Paths.LiveKitConfig -Content $rendered
    Set-EV211RestrictedAcl -Path $Paths.LiveKitConfig
}

# ---- WinSW 서비스 정의 -----------------------------------------------------------------------

function ConvertTo-EV211Xml {
    param([string]$Value)
    return [System.Security.SecurityElement]::Escape([string]$Value)
}

function New-EV211WinSWXml {
    param(
        [string]$Id, [string]$Name, [string]$Description,
        [string]$Executable, [string]$Arguments, [string]$WorkingDirectory,
        $Environment, [string[]]$Depends, [string]$LogPath
    )
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine('<?xml version="1.0" encoding="utf-8"?>')
    [void]$sb.AppendLine('<!-- install.ps1 / ev211ctl.ps1 restart 가 ev211.env 로부터 생성한다. 직접 수정하면 다음 재시작 때 덮어쓴다. -->')
    [void]$sb.AppendLine('<service>')
    [void]$sb.AppendLine("  <id>$(ConvertTo-EV211Xml $Id)</id>")
    [void]$sb.AppendLine("  <name>$(ConvertTo-EV211Xml $Name)</name>")
    [void]$sb.AppendLine("  <description>$(ConvertTo-EV211Xml $Description)</description>")
    [void]$sb.AppendLine("  <executable>$(ConvertTo-EV211Xml $Executable)</executable>")
    [void]$sb.AppendLine("  <arguments>$(ConvertTo-EV211Xml $Arguments)</arguments>")
    [void]$sb.AppendLine("  <workingdirectory>$(ConvertTo-EV211Xml $WorkingDirectory)</workingdirectory>")
    # 부팅 자동 시작. 지연 시작으로 네트워크(DHCP 예약 IP) 준비 후 기동해 node-ip 불일치를 줄인다.
    [void]$sb.AppendLine('  <startmode>Automatic</startmode>')
    [void]$sb.AppendLine('  <delayedAutoStart>true</delayedAutoStart>')
    foreach ($dep in @($Depends)) {
        if ($dep) { [void]$sb.AppendLine("  <depend>$(ConvertTo-EV211Xml $dep)</depend>") }
    }
    # 도커 restart: unless-stopped 대응 — 비정상 종료 시 5s/10s/30s 후 재시작, 1시간 후 실패 횟수 초기화.
    [void]$sb.AppendLine('  <onfailure action="restart" delay="5 sec"/>')
    [void]$sb.AppendLine('  <onfailure action="restart" delay="10 sec"/>')
    [void]$sb.AppendLine('  <onfailure action="restart" delay="30 sec"/>')
    [void]$sb.AppendLine('  <resetfailure>1 hour</resetfailure>')
    [void]$sb.AppendLine('  <stoptimeout>20 sec</stoptimeout>')
    foreach ($key in @($Environment.Keys)) {
        [void]$sb.AppendLine("  <env name=""$(ConvertTo-EV211Xml $key)"" value=""$(ConvertTo-EV211Xml $Environment[$key])""/>")
    }
    [void]$sb.AppendLine("  <logpath>$(ConvertTo-EV211Xml $LogPath)</logpath>")
    # 10MB 단위 회전, 8개 보관(도커 json-file 기본보다 보수적으로 상한을 둔다).
    [void]$sb.AppendLine('  <log mode="roll-by-size">')
    [void]$sb.AppendLine('    <sizeThreshold>10240</sizeThreshold>')
    [void]$sb.AppendLine('    <keepFiles>8</keepFiles>')
    [void]$sb.AppendLine('  </log>')
    [void]$sb.AppendLine('</service>')
    return $sb.ToString()
}

# 3개 서비스의 WinSW 실행 파일(<id>.exe)과 XML 을 services\ 에 생성한다.
# 비밀(키·비밀번호)은 XML 에 넣지 않는다 — field-api 는 uvicorn --env-file 로 ev211.env 를 직접 읽는다.
function Write-EV211ServiceFiles {
    param([Parameter(Mandatory = $true)]$Paths, [Parameter(Mandatory = $true)]$EnvMap)
    $nodeIp = Get-EV211EnvValue $EnvMap 'FIELD_NODE_IP' '127.0.0.1'
    if (-not (Test-EV211IPv4 $nodeIp)) { throw "FIELD_NODE_IP 가 IPv4 주소가 아닙니다: $nodeIp" }

    if (-not (Test-Path -LiteralPath $Paths.WinSWExe)) { throw "WinSW 실행 파일이 없습니다: $($Paths.WinSWExe)" }
    foreach ($id in $script:EV211ServiceIds) {
        Copy-Item -LiteralPath $Paths.WinSWExe -Destination (Join-Path $Paths.Services "$id.exe") -Force
    }

    # 1) LiveKit — 도커판 `--config <렌더링본> --node-ip ${FIELD_NODE_IP}` 와 동일.
    $livekitEnv = [ordered]@{}
    $xml = New-EV211WinSWXml -Id 'ev211-livekit' -Name 'EV211 Field LiveKit' `
        -Description 'EV211 내부망 오디오 SFU(livekit-server). signaling 7880/tcp, TCP 폴백 7881, RTC UDP 범위.' `
        -Executable $Paths.LiveKitExe `
        -Arguments ('--config "' + $Paths.LiveKitConfig + '" --node-ip ' + $nodeIp) `
        -WorkingDirectory $Paths.Config -Environment $livekitEnv -Depends @() -LogPath $Paths.Logs
    Write-EV211Utf8File -Path (Join-Path $Paths.Services 'ev211-livekit.xml') -Content $xml

    # 2) field-api — Dockerfile CMD 와 동일 옵션. 단 바인딩은 0.0.0.0 이 아니라 127.0.0.1(Caddy 뒤 전용).
    #    --workers 1 고정: SQLite 단일 연결·메모리 상태 공유. 또한 uvicorn 은 workers>1 이면 Windows 에서
    #    SelectorEventLoop 로 바꿔 asyncio 서브프로세스(lame 녹음)가 동작하지 않는다.
    #    신뢰 프록시는 Caddy(127.0.0.1) 하나뿐이므로 사설 대역 전체가 아니라 127.0.0.1 만 신뢰한다.
    $apiEnv = [ordered]@{
        'FIELD_DB_PATH'             = $Paths.DbPath
        'FIELD_RECORDINGS_PATH'     = $Paths.Recordings
        'LIVEKIT_HOST'              = 'http://127.0.0.1:7880'
        'LIVEKIT_RTC_URL'           = 'ws://127.0.0.1:7880'
        'FORWARDED_ALLOW_IPS'       = '127.0.0.1'
        'FIELD_FORWARDED_ALLOW_IPS' = '127.0.0.1'
        # Linux 와 같게 stdout/파일 기본 인코딩을 UTF-8 로(로그 한글 깨짐 방지).
        'PYTHONUTF8'                = '1'
        # lame.exe 를 shutil.which('lame') 가 찾도록 bin\lame 을 PATH 앞에 둔다.
        'PATH'                      = ($Paths.LameDir + ';%PATH%')
    }
    $apiArgs = '-m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 ' +
        '--forwarded-allow-ips 127.0.0.1 --env-file "' + $Paths.EnvFile + '"'
    $xml = New-EV211WinSWXml -Id 'ev211-api' -Name 'EV211 Field API' `
        -Description 'EV211 field-api(FastAPI/uvicorn) 제어 평면. 127.0.0.1:8000 루프백 전용, Caddy 뒤에서만 접근.' `
        -Executable $Paths.VenvPython -Arguments $apiArgs -WorkingDirectory $Paths.ApiDir `
        -Environment $apiEnv -Depends @('ev211-livekit') -LogPath $Paths.Logs
    Write-EV211Utf8File -Path (Join-Path $Paths.Services 'ev211-api.xml') -Content $xml

    # 3) Caddy — 정적 웹 + /rtc·/api 프록시. 웹 루트는 Caddyfile 토큰 안전을 위해 슬래시 경로로 넘긴다.
    $caddyEnv = [ordered]@{
        'FIELD_NODE_IP'   = $nodeIp
        'FIELD_WEB_ROOT'  = ($Paths.WebDir -replace '\\', '/')
        'XDG_DATA_HOME'   = $Paths.CaddyData
        'XDG_CONFIG_HOME' = $Paths.CaddyConfig
    }
    $caddyArgs = 'run --config "' + $Paths.Caddyfile + '" --adapter caddyfile'
    $xml = New-EV211WinSWXml -Id 'ev211-caddy' -Name 'EV211 Field Caddy' `
        -Description 'EV211 정적 웹·자체서명 HTTPS(8443)·루프백 HTTP(127.0.0.1:8880)·80 리다이렉트.' `
        -Executable $Paths.CaddyExe -Arguments $caddyArgs -WorkingDirectory $Paths.App `
        -Environment $caddyEnv -Depends @('ev211-api') -LogPath $Paths.Logs
    Write-EV211Utf8File -Path (Join-Path $Paths.Services 'ev211-caddy.xml') -Content $xml
}

# 설정 재생성(livekit.yaml + 서비스 XML). WinSW 는 서비스 시작 때마다 XML 을 다시 읽으므로
# 서비스 중지 → 재생성 → 시작이면 ev211.env 변경이 반영된다(도커판 `compose up -d` 와 같은 효과).
function Update-EV211RuntimeConfig {
    param([Parameter(Mandatory = $true)]$Paths)
    $envMap = Read-EV211EnvFile -Path $Paths.EnvFile
    Write-EV211LiveKitConfig -Paths $Paths -EnvMap $envMap
    Write-EV211ServiceFiles -Paths $Paths -EnvMap $envMap
    return $envMap
}

# ---- 서비스 제어 -----------------------------------------------------------------------------

function Get-EV211Service {
    param([string]$Id)
    return (Get-Service -Name $Id -ErrorAction SilentlyContinue)
}

function Wait-EV211ServiceStatus {
    param([string]$Id, [string]$Status, [int]$TimeoutSeconds = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $service = Get-EV211Service $Id
        if ($null -eq $service) { return ($Status -eq 'Missing') }
        if ([string]$service.Status -eq $Status) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Stop-EV211Services {
    $ids = @($script:EV211ServiceIds)
    [array]::Reverse($ids)
    foreach ($id in $ids) {
        $service = Get-EV211Service $id
        if ($null -ne $service -and [string]$service.Status -ne 'Stopped') {
            Write-Host "중지: $id"
            Stop-Service -Name $id -Force -ErrorAction Continue
            if (-not (Wait-EV211ServiceStatus -Id $id -Status 'Stopped' -TimeoutSeconds 60)) {
                Write-EV211Warn "$id 가 60초 안에 중지되지 않았습니다."
            }
        }
    }
}

function Start-EV211Services {
    foreach ($id in $script:EV211ServiceIds) {
        $service = Get-EV211Service $id
        if ($null -eq $service) { throw "서비스가 설치되어 있지 않습니다: $id (install.ps1 을 먼저 실행하세요)" }
        if ([string]$service.Status -ne 'Running') {
            Write-Host "시작: $id"
            Start-Service -Name $id
        }
    }
}

# WinSW 로 서비스를 (재)등록한다. 기존 등록이 있으면 중지·삭제 후 다시 등록해 depend·startmode 변경을 반영한다.
function Install-EV211Services {
    param([Parameter(Mandatory = $true)]$Paths)
    Stop-EV211Services
    Uninstall-EV211Services -Paths $Paths
    foreach ($id in $script:EV211ServiceIds) {
        $exe = Join-Path $Paths.Services "$id.exe"
        Write-Host "등록: $id"
        & $exe install
        Assert-EV211ExitCode "$id 서비스 등록"
    }
}

function Uninstall-EV211Services {
    param([Parameter(Mandatory = $true)]$Paths)
    $ids = @($script:EV211ServiceIds)
    [array]::Reverse($ids)
    foreach ($id in $ids) {
        if ($null -eq (Get-EV211Service $id)) { continue }
        $exe = Join-Path $Paths.Services "$id.exe"
        if (Test-Path -LiteralPath $exe) {
            & $exe uninstall
        }
        else {
            & "$env:SystemRoot\System32\sc.exe" delete $id | Out-Null
        }
        if (-not (Wait-EV211ServiceStatus -Id $id -Status 'Missing' -TimeoutSeconds 30)) {
            throw "$id 서비스 삭제가 끝나지 않았습니다. 서비스 관리 콘솔(services.msc)을 닫고 다시 실행하세요."
        }
    }
}

# ---- 방화벽 ----------------------------------------------------------------------------------

# docs/INSTALL.md 의 UFW 규칙과 같은 포트. 신뢰 내부망 전제이므로 '개인(Private)' 프로필에만 연다.
# 8880 은 루프백 전용이라 열지 않고, 8000(field-api)도 루프백 바인딩이라 열지 않는다.
function Set-EV211FirewallRules {
    param([int]$UdpStart, [int]$UdpEnd)
    Remove-EV211FirewallRules
    $rules = @(
        @{ Name = 'EV211-Field-HTTP-80'; Display = 'EV211 Field - HTTP 리다이렉트 (TCP 80)'; Protocol = 'TCP'; Port = '80' },
        @{ Name = 'EV211-Field-HTTPS-8443'; Display = 'EV211 Field - HTTPS 웹/API (TCP 8443)'; Protocol = 'TCP'; Port = '8443' },
        @{ Name = 'EV211-Field-LiveKit-7880'; Display = 'EV211 Field - LiveKit signaling (TCP 7880)'; Protocol = 'TCP'; Port = '7880' },
        @{ Name = 'EV211-Field-LiveKit-7881'; Display = 'EV211 Field - LiveKit TCP 미디어 폴백 (TCP 7881)'; Protocol = 'TCP'; Port = '7881' },
        @{ Name = 'EV211-Field-LiveKit-UDP'; Display = "EV211 Field - LiveKit RTC 미디어 (UDP $UdpStart-$UdpEnd)"; Protocol = 'UDP'; Port = "$UdpStart-$UdpEnd" }
    )
    foreach ($rule in $rules) {
        New-NetFirewallRule -Name $rule.Name -DisplayName $rule.Display -Group $script:EV211FirewallGroup `
            -Direction Inbound -Action Allow -Protocol $rule.Protocol -LocalPort $rule.Port `
            -Profile Private -Enabled True | Out-Null
        Write-Host "방화벽 허용(개인 프로필): $($rule.Display)"
    }
}

function Remove-EV211FirewallRules {
    $existing = @(Get-NetFirewallRule -Group $script:EV211FirewallGroup -ErrorAction SilentlyContinue)
    if ($existing.Count -gt 0) { $existing | Remove-NetFirewallRule }
}

# ---- 진단 보조 -------------------------------------------------------------------------------

# 이미 LISTEN 중인 TCP 포트를 찾는다(IIS·http.sys 가 80 을 점유하는 경우가 흔함).
function Get-EV211PortConflicts {
    param([int[]]$Ports)
    $conflicts = @()
    foreach ($port in $Ports) {
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
        foreach ($listener in $listeners) {
            $owner = '(알 수 없음)'
            if ($listener.OwningProcess -eq 4) {
                $owner = 'System(http.sys — IIS/WinRM 등. netsh http show servicestate 로 확인)'
            }
            else {
                $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
                if ($process) { $owner = "$($process.ProcessName) (PID $($listener.OwningProcess))" }
            }
            $conflicts += "TCP $port ($($listener.LocalAddress)) ← $owner"
        }
    }
    return $conflicts
}

function Invoke-EV211Curl {
    param([string]$Url, [int]$MaxTime = 2)
    # PowerShell 5.1 의 curl 은 Invoke-WebRequest 별칭이므로 curl.exe(Windows 10 1803+ 기본 포함)를 명시한다.
    # -S 를 빼 stderr 출력을 없앤다(PS 5.1 + ErrorActionPreference=Stop 에서 2>$null 리다이렉트는
    # 네이티브 stderr 를 오류 레코드로 바꿔 스크립트를 중단시킬 수 있다).
    & "$env:SystemRoot\System32\curl.exe" -kfs --max-time $MaxTime -o NUL $Url
    return ($LASTEXITCODE -eq 0)
}

function Wait-EV211Url {
    param([string]$Url, [int]$Attempts = 30)
    for ($i = 0; $i -lt $Attempts; $i++) {
        if (Invoke-EV211Curl -Url $Url) { return $true }
        Start-Sleep -Seconds 1
    }
    Write-Host "서비스 준비 시간을 초과했습니다: $Url" -ForegroundColor Red
    return $false
}

function Get-EV211Tar {
    $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
    if (-not (Test-Path -LiteralPath $tar)) { throw 'tar.exe 가 없습니다(Windows 10 1803 이상 필요).' }
    return $tar
}
