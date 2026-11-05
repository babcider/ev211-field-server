<#
.SYNOPSIS
  EV211 Field Server(Windows 네이티브) 운영 명령 — scripts/ev211ctl(Linux) 의 Windows 대응판.

.DESCRIPTION
  사용법(관리자 PowerShell 또는 관리자 cmd):
    <Root>\ev211ctl.cmd <명령> [인자]
    powershell -NoProfile -ExecutionPolicy Bypass -File <Root>\app\windows\ev211ctl.ps1 <명령> [인자]

  명령:
    start | stop | restart        서비스 시작·중지·재시작(restart 는 ev211.env 를 다시 렌더링해 반영)
    status                        서비스 상태
    doctor                        종합 진단(서비스·IP·방화벽·네트워크 프로필·HTTP 헬스체크)
    logs [서비스] [-Tail N] [-Follow]   로그 보기(서비스: livekit|api|caddy, 생략 시 전체 마지막 N줄)
    signal-logs [N]               최근 송수신 신호 이벤트 N건(기본 200, 1~10000)
    credentials                   서버 IP·송신/관리자 비밀번호
    detect-ip [-Write]            LAN IP 감지(-Write: ev211.env 의 FIELD_NODE_IP·FIELD_WS_URL 갱신)
    export-ca [경로]              Caddy 내부 루트 CA 인증서(공개 인증서) 내보내기
    backup [폴더]                 서비스 중지 → 설정·DB·녹음·Caddy 데이터 백업 → 재시작
    restore <백업.tar.gz> -Yes    백업 복구(현재 데이터 덮어씀)
    update                        (미구현 TODO) — README 의 수동 업데이트 절차 참고
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Command = 'help',
    [Parameter(Position = 1)][string]$Arg1 = '',
    [string]$Root = '',
    [int]$Tail = 200,
    [switch]$Follow,
    [switch]$Write,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'ev211-common.ps1')

# 설치본(<Root>\app\windows\ev211ctl.ps1)에서 실행되면 그 Root 를 우선 사용한다.
if (-not $Root) {
    $installedRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    if (Test-Path -LiteralPath (Join-Path $installedRoot 'config\ev211.env')) { $Root = $installedRoot }
    else { $Root = Get-EV211DefaultRoot }
}
$P = Get-EV211Paths -Root $Root
# 함수는 값을 return 하지 않고 이 변수에 종료 코드를 남긴다. PowerShell 함수 안의 네이티브 명령 출력
# (curl·python)은 반환값에 섞여 들어가므로 "return 코드" 패턴을 쓰면 출력이 사라지거나 코드가 배열이 된다.
$script:ExitCode = 0

function Show-Usage {
    Write-Host '사용법: ev211ctl {start|stop|restart|status|doctor|logs|signal-logs|credentials|detect-ip|export-ca|backup|restore|update}'
    Write-Host '  자세한 설명: Get-Help <Root>\app\windows\ev211ctl.ps1 -Detailed'
}

function Assert-Installed {
    if (-not (Test-Path -LiteralPath $P.EnvFile)) { throw "설정 파일을 찾을 수 없습니다: $($P.EnvFile) (install.ps1 을 먼저 실행하세요)" }
    if (-not (Test-Path -LiteralPath $P.Services)) { throw "서비스 폴더를 찾을 수 없습니다: $($P.Services)" }
}

function Show-Status {
    $rows = foreach ($id in $script:EV211ServiceIds) {
        $service = Get-EV211Service $id
        if ($null -eq $service) {
            [pscustomobject]@{ Service = $id; Status = '미설치'; StartType = '-'; PID = '-' }
        }
        else {
            $cim = Get-CimInstance -ClassName Win32_Service -Filter "Name='$id'" -ErrorAction SilentlyContinue
            $processId = '-'
            if ($cim -and $cim.ProcessId) { $processId = $cim.ProcessId }
            [pscustomobject]@{ Service = $id; Status = [string]$service.Status; StartType = [string]$service.StartType; PID = $processId }
        }
    }
    $rows | Format-Table -AutoSize | Out-String | Write-Host
}

function Restart-Server {
    Stop-EV211Services
    Update-EV211RuntimeConfig -Paths $P | Out-Null
    Start-EV211Services
}

function Invoke-Doctor {
    $failed = 0
    $envMap = Read-EV211EnvFile -Path $P.EnvFile
    $nodeIp = Get-EV211EnvValue $envMap 'FIELD_NODE_IP' '127.0.0.1'
    $udpStart = [int](Get-EV211EnvValue $envMap 'FIELD_UDP_START' '50000')
    $udpEnd = [int](Get-EV211EnvValue $envMap 'FIELD_UDP_END' '60000')

    Write-EV211Step '서비스'
    Show-Status
    foreach ($id in $script:EV211ServiceIds) {
        $service = Get-EV211Service $id
        if ($null -eq $service -or [string]$service.Status -ne 'Running') {
            Write-Host "실패: $id 가 실행 중이 아닙니다." -ForegroundColor Red
            $failed++
        }
    }

    Write-EV211Step "서버 IP ($nodeIp)"
    $local = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -eq $nodeIp })
    if ($local.Count -eq 0) {
        Write-Host "실패: FIELD_NODE_IP=$nodeIp 가 이 PC 의 어떤 어댑터에도 없습니다(IP 변경?). 'ev211ctl detect-ip -Write' 후 'ev211ctl restart'." -ForegroundColor Red
        $failed++
    }
    else {
        $netProfile = Get-NetConnectionProfile -InterfaceIndex $local[0].InterfaceIndex -ErrorAction SilentlyContinue
        $category = '알 수 없음'
        if ($netProfile) { $category = [string]$netProfile.NetworkCategory }
        Write-Host "어댑터: $($local[0].InterfaceAlias), 네트워크 프로필: $category"
        if ($category -eq 'Public') {
            Write-Host '실패: 네트워크 프로필이 "공용(Public)" 입니다. EV211 방화벽 규칙은 "개인(Private)" 에만 열립니다.' -ForegroundColor Red
            Write-Host "       Set-NetConnectionProfile -InterfaceIndex $($local[0].InterfaceIndex) -NetworkCategory Private"
            $failed++
        }
        if ($local[0].PrefixOrigin -eq 'Dhcp') {
            Write-EV211Warn 'DHCP 주소입니다. 공유기에서 DHCP 예약(고정 할당)을 설정하세요(IP 가 바뀌면 단말 접속 실패).'
        }
    }

    Write-EV211Step '방화벽'
    $rules = @(Get-NetFirewallRule -Group $script:EV211FirewallGroup -ErrorAction SilentlyContinue | Where-Object { $_.Enabled -eq 'True' })
    if ($rules.Count -lt 5) {
        Write-Host "실패: EV211 방화벽 규칙이 $($rules.Count)/5 개만 있습니다(install.ps1 재실행)." -ForegroundColor Red
        $failed++
    }
    else {
        Write-Host "EV211 규칙 $($rules.Count) 개 활성(개인 프로필)."
    }

    Write-EV211Step '녹음 인코더(lame)'
    if (Test-Path -LiteralPath (Join-Path $P.LameDir 'lame.exe')) { Write-Host "lame.exe: $($P.LameDir)" }
    else { Write-Host "실패: $($P.LameDir)\lame.exe 가 없습니다(녹음 불가)." -ForegroundColor Red; $failed++ }

    Write-EV211Step "RTC UDP 범위 ($udpStart-$udpEnd)"
    try {
        $dyn = Get-NetUDPSetting -ErrorAction Stop
        $dynStart = [int]$dyn.DynamicPortRangeStartPort
        $dynEnd = $dynStart + [int]$dyn.DynamicPortRangeNumberOfPorts - 1
        if ($udpStart -le $dynEnd -and $udpEnd -ge $dynStart) {
            Write-Host "참고: Windows UDP 동적 포트($dynStart-$dynEnd)와 겹칩니다. LiveKit 은 사용 중 포트를 건너뛰므로 보통 문제없지만,"
            Write-Host '      부하 테스트에서 ICE 실패가 보이면 FIELD_UDP_START/END 를 40000/45000 으로 바꾸고 install.ps1 을 다시 실행하세요.'
        }
    }
    catch { Write-Verbose "무시(폴백 진행): $_" }

    Write-EV211Step 'HTTP 헬스체크'
    foreach ($url in @('http://127.0.0.1:7880/', 'http://localhost:8880/api/healthz', 'https://localhost:8443/api/healthz')) {
        if (Wait-EV211Url -Url $url) { Write-Host "OK  $url" }
        else { $failed++ }
    }
    if ($nodeIp -ne '127.0.0.1') {
        $lanUrl = "https://${nodeIp}:8443/api/healthz"
        if (Invoke-EV211Curl -Url $lanUrl -MaxTime 3) { Write-Host "OK  $lanUrl" }
        else { Write-Host "실패: $lanUrl (LAN IP 경로)" -ForegroundColor Red; $failed++ }
    }
    & "$env:SystemRoot\System32\curl.exe" -fs --max-time 8 'http://localhost:8880/api/healthz' | Out-Host
    Write-Host ''

    if ($failed -gt 0) {
        Write-Host "진단 실패 $failed 건" -ForegroundColor Red
        $script:ExitCode = 1
        return
    }
    Write-Host '진단 통과' -ForegroundColor Green
}

function Show-Logs {
    param([string]$Service)
    $map = @{ 'livekit' = 'ev211-livekit'; 'api' = 'ev211-api'; 'field-api' = 'ev211-api'; 'caddy' = 'ev211-caddy' }
    $ids = @($script:EV211ServiceIds)
    if ($Service) {
        if ($map.ContainsKey($Service)) { $ids = @($map[$Service]) }
        elseif ($script:EV211ServiceIds -contains $Service) { $ids = @($Service) }
        else { throw "알 수 없는 서비스: $Service (livekit|api|caddy)" }
    }
    if ($Follow) {
        if ($ids.Count -ne 1) { throw '-Follow 는 서비스를 하나 지정해야 합니다. 예: ev211ctl logs api -Follow' }
        # uvicorn·caddy 는 stderr, livekit 은 stdout 에 주로 쓴다 → 최근에 갱신된 쪽을 따라간다.
        $candidates = @(Get-ChildItem -LiteralPath $P.Logs -File -Filter "$($ids[0]).*.log" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '\.(out|err)\.log$' } | Sort-Object LastWriteTime -Descending)
        if ($candidates.Count -eq 0) { throw "로그 파일이 없습니다: $($P.Logs)\$($ids[0]).*.log" }
        Write-Host "따라가는 중: $($candidates[0].FullName) (Ctrl+C 로 종료)"
        Get-Content -LiteralPath $candidates[0].FullName -Tail $Tail -Wait -Encoding UTF8
        return
    }
    foreach ($id in $ids) {
        foreach ($kind in @('wrapper', 'out', 'err')) {
            $file = Join-Path $P.Logs "$id.$kind.log"
            if (Test-Path -LiteralPath $file) {
                Write-Host "----- $file -----" -ForegroundColor DarkCyan
                Get-Content -LiteralPath $file -Tail $Tail -Encoding UTF8
            }
        }
    }
}

# Linux 판과 같은 SQL·출력 형식. 스크립트는 ASCII 만 사용(PowerShell 5.1 파이프 인코딩이 ASCII).
$script:SignalLogsPython = @'
import datetime, pathlib, sqlite3, sys
limit = int(sys.argv[1])
uri = pathlib.Path(sys.argv[2]).as_uri() + "?mode=ro"
connection = sqlite3.connect(uri, uri=True)
connection.row_factory = sqlite3.Row
rows = connection.execute(
    "SELECT occurred_at, direction, event_type, scope, channel_id, generation, "
    "room, track_name, subject_hash, client_ip "
    "FROM signal_events ORDER BY occurred_at DESC, id DESC LIMIT ?",
    (limit,),
).fetchall()
connection.close()
print("occurred_at_utc\tdirection\tevent\tscope\tchannel\tgeneration\troom\ttrack\tsubject_hash\tclient_ip")
for row in rows:
    occurred_at = datetime.datetime.fromtimestamp(row["occurred_at"], datetime.timezone.utc).isoformat(timespec="seconds")
    values = [occurred_at, row["direction"], row["event_type"], row["scope"], row["channel_id"],
              row["generation"], row["room"], row["track_name"], row["subject_hash"], row["client_ip"]]
    print("\t".join("-" if value is None else str(value) for value in values))
'@

function Show-SignalLogs {
    param([string]$Limit)
    if (-not $Limit) { $Limit = '200' }
    if ($Limit -notmatch '^\d+$' -or [int64]$Limit -lt 1 -or [int64]$Limit -gt 10000) {
        Write-Host '조회 개수는 1~10000 사이 정수여야 합니다.' -ForegroundColor Red
        $script:ExitCode = 2
        return
    }
    if (-not (Test-Path -LiteralPath $P.DbPath)) { throw "DB 파일이 없습니다: $($P.DbPath)" }
    # 서비스와 같은 venv Python 으로 읽기 전용 조회(실행 중에도 안전).
    $script:SignalLogsPython | & $P.VenvPython - $Limit $P.DbPath | Out-Host
    if ($LASTEXITCODE -ne 0) { $script:ExitCode = $LASTEXITCODE }
}

function Show-Credentials {
    $envMap = Read-EV211EnvFile -Path $P.EnvFile
    Write-Host "서버 IP: $(Get-EV211EnvValue $envMap 'FIELD_NODE_IP' '')"
    Write-Host "송신 비밀번호: $(Get-EV211EnvValue $envMap 'FIELD_SEND_PASSWORD' '')"
    Write-Host "관리자 비밀번호: $(Get-EV211EnvValue $envMap 'FIELD_ADMIN_PASSWORD' '')"
}

function Invoke-DetectIp {
    $ip = Get-EV211LanIp
    if (-not $ip) {
        Write-Host 'LAN IP 감지 실패. ev211.env 의 FIELD_NODE_IP 를 수동으로 설정하세요.' -ForegroundColor Red
        $script:ExitCode = 1
        return
    }
    if ($Write) {
        Set-EV211EnvValue -Path $P.EnvFile -Key 'FIELD_NODE_IP' -Value $ip
        Set-EV211EnvValue -Path $P.EnvFile -Key 'FIELD_WS_URL' -Value "ws://${ip}:7880"
        Write-Host "ev211.env 갱신: FIELD_NODE_IP=$ip, FIELD_WS_URL=ws://${ip}:7880 — 'ev211ctl restart' 로 반영하세요."
    }
    else {
        Write-Host $ip
    }
}

function Export-RootCa {
    param([string]$Destination)
    $source = Join-Path $P.CaddyData 'caddy\pki\authorities\local\root.crt'
    if (-not (Test-Path -LiteralPath $source)) { throw "루트 CA 가 아직 없습니다: $source (Caddy 가 한 번 기동돼야 생성)" }
    if (-not $Destination) { $Destination = Join-Path (Get-Location).Path 'field-root-ca.crt' }
    # 공개 인증서(root.crt)만 내보낸다. 같은 폴더의 root.key(개인키)는 절대 복사하지 않는다.
    Copy-Item -LiteralPath $source -Destination $Destination -Force
    Write-Host "내보냄: $Destination (단말에 '신뢰할 수 있는 루트 인증 기관' 으로 설치)"
}

# ---- 백업·복구 (Linux 판과 같은 아카이브 구조: ev211.env + api-data/caddy-data/caddy-config.tar.gz) ----

function Invoke-Backup {
    param([string]$Destination)
    if (-not $Destination) { $Destination = $P.Backups }
    if (-not (Test-Path -LiteralPath $Destination)) { New-Item -ItemType Directory -Path $Destination | Out-Null }
    $tar = Get-EV211Tar
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
    $archive = Join-Path $Destination "ev211-field-$stamp.tar.gz"
    $work = Join-Path $P.Root ('.backup-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    Stop-EV211Services
    try {
        Copy-Item -LiteralPath $P.EnvFile -Destination (Join-Path $work 'ev211.env')
        # api-data: Linux 의 /data 볼륨과 같은 배치(field.db[-wal|-shm] + recordings/).
        $apiItems = @(Get-ChildItem -LiteralPath $P.Data -File -Filter 'field.db*' | ForEach-Object { $_.Name })
        $tarArgs = @('-czf', (Join-Path $work 'api-data.tar.gz'))
        if ($apiItems.Count -gt 0) { $tarArgs += @('-C', $P.Data) + $apiItems }
        $tarArgs += @('-C', $P.Root, 'recordings')
        & $tar @tarArgs
        Assert-EV211ExitCode 'api-data 압축'
        & $tar -czf (Join-Path $work 'caddy-data.tar.gz') -C $P.CaddyData .
        Assert-EV211ExitCode 'caddy-data 압축'
        & $tar -czf (Join-Path $work 'caddy-config.tar.gz') -C $P.CaddyConfig .
        Assert-EV211ExitCode 'caddy-config 압축'
        & $tar -czf $archive -C $work 'ev211.env' 'api-data.tar.gz' 'caddy-data.tar.gz' 'caddy-config.tar.gz'
        Assert-EV211ExitCode '백업 아카이브 생성'
        if ($Destination -ne $P.Backups) { Set-EV211RestrictedAcl -Path $archive }
    }
    finally {
        Start-EV211Services
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host $archive
}

function Assert-SafeArchive {
    param([string]$Archive)
    $tar = Get-EV211Tar
    $entries = @(& $tar -tzf $Archive)
    Assert-EV211ExitCode "아카이브 목록 읽기($Archive)"
    foreach ($entry in $entries) {
        if ($entry -match '^(/|\\|[A-Za-z]:)' -or $entry -match '(^|[\\/])\.\.($|[\\/])') {
            throw "안전하지 않은 경로가 포함된 백업입니다: $entry"
        }
    }
}

function Clear-Directory {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) { Get-ChildItem -LiteralPath $Path -Force | Remove-Item -Recurse -Force }
    else { New-Item -ItemType Directory -Path $Path | Out-Null }
}

function Invoke-Restore {
    param([string]$Archive)
    if (-not $Archive -or -not $Yes) {
        Write-Host '사용법: ev211ctl restore <백업.tar.gz> -Yes' -ForegroundColor Red
        $script:ExitCode = 2
        return
    }
    $Archive = (Resolve-Path -LiteralPath $Archive).Path
    $tar = Get-EV211Tar
    Assert-SafeArchive -Archive $Archive
    $work = Join-Path $P.Root ('.restore-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    $stopped = $false
    $ok = $false
    try {
        & $tar -xzf $Archive -C $work
        Assert-EV211ExitCode '백업 압축 해제'
        foreach ($required in @('ev211.env', 'api-data.tar.gz', 'caddy-data.tar.gz', 'caddy-config.tar.gz')) {
            if (-not (Test-Path -LiteralPath (Join-Path $work $required))) { throw "백업에 $required 파일이 없습니다." }
            if ($required -like '*.tar.gz') { Assert-SafeArchive -Archive (Join-Path $work $required) }
        }
        Stop-EV211Services
        $stopped = $true

        Copy-Item -LiteralPath (Join-Path $work 'ev211.env') -Destination $P.EnvFile -Force
        Set-EV211RestrictedAcl -Path $P.EnvFile

        $apiStage = Join-Path $work 'api-data'
        New-Item -ItemType Directory -Path $apiStage | Out-Null
        & $tar -xzf (Join-Path $work 'api-data.tar.gz') -C $apiStage
        Assert-EV211ExitCode 'api-data 해제'
        Get-ChildItem -LiteralPath $P.Data -File -Filter 'field.db*' | Remove-Item -Force
        Get-ChildItem -LiteralPath $apiStage -File -Filter 'field.db*' | Copy-Item -Destination $P.Data -Force
        Clear-Directory -Path $P.Recordings
        $stageRecordings = Join-Path $apiStage 'recordings'
        if (Test-Path -LiteralPath $stageRecordings) {
            Get-ChildItem -LiteralPath $stageRecordings -Force | Copy-Item -Destination $P.Recordings -Recurse -Force
        }
        Clear-Directory -Path $P.CaddyData
        & $tar -xzf (Join-Path $work 'caddy-data.tar.gz') -C $P.CaddyData
        Assert-EV211ExitCode 'caddy-data 해제'
        Clear-Directory -Path $P.CaddyConfig
        & $tar -xzf (Join-Path $work 'caddy-config.tar.gz') -C $P.CaddyConfig
        Assert-EV211ExitCode 'caddy-config 해제'

        # 다른 PC(또는 Linux 서버) 백업이면 IP·경로가 다를 수 있다 — 경로는 서비스 XML 이 고정 주입하고,
        # IP 는 doctor 가 불일치를 알려준다. 설정을 다시 렌더링한 뒤 기동한다.
        Update-EV211RuntimeConfig -Paths $P | Out-Null
        Start-EV211Services
        $ok = $true
    }
    finally {
        if ($stopped -and -not $ok) {
            try { Start-EV211Services } catch { Write-EV211Warn "서비스 재시작 실패: $_" }
        }
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
    Invoke-Doctor
}

# ---- 진입점 ------------------------------------------------------------------------------------

switch ($Command.ToLowerInvariant()) {
    { $_ -in @('help', '-h', '--help', '/?') } { Show-Usage; exit 0 }
}
Assert-EV211Administrator
Assert-Installed
switch ($Command.ToLowerInvariant()) {
    'start' { Start-EV211Services }
    'stop' { Stop-EV211Services }
    'restart' { Restart-Server }
    'status' { Show-Status }
    'doctor' { Invoke-Doctor }
    'logs' { Show-Logs -Service $Arg1 }
    'signal-logs' { Show-SignalLogs -Limit $Arg1 }
    'credentials' { Show-Credentials }
    'detect-ip' { Invoke-DetectIp }
    'export-ca' { Export-RootCa -Destination $Arg1 }
    'backup' { Invoke-Backup -Destination $Arg1 }
    'restore' { Invoke-Restore -Archive $Arg1 }
    'update' {
        # TODO(Windows): Linux 판 update(백업 → git pull --ff-only → 재기동 → doctor 실패 시 롤백)에 대응하는
        #   자동 업데이트. PoC 범위 밖 — README 의 "업데이트(수동)" 절차(백업 → 저장소 갱신 → install.ps1 재실행)를 따른다.
        Write-Host 'update 는 Windows PoC 에서 아직 지원하지 않습니다. windows\README.md 의 "업데이트(수동)" 절차를 따르세요.' -ForegroundColor Yellow
        $script:ExitCode = 2
    }
    default { Show-Usage; $script:ExitCode = 2 }
}
exit $script:ExitCode
