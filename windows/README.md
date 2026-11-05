# Windows 네이티브 설치 키트 (PoC)

도커 없이 Windows 10/11 x64 PC 한 대에서 EV211 Field Server(LiveKit SFU + field-api + Caddy + lame 녹음)를 **Windows 서비스 3개**로 실행합니다.

> **상태: PoC(개념 증명).** macOS 에서 정적 검증(구문·설정 렌더링·Caddy 설정 검증)만 했고 실제 Windows 기기 검증은 아직입니다. 행사 투입 전에는 반드시 아래 [실기 검증 체크리스트](#실기-검증-체크리스트)와 부하 테스트를 통과해야 합니다. 운영 기준 환경은 계속 [Ubuntu + Docker](../docs/INSTALL.md)입니다.

## 구성

| 서비스 (WinSW ID) | 실행 파일 | 리슨 | 도커판 대응 |
|---|---|---|---|
| `ev211-livekit` | `livekit-server.exe --config config\livekit.yaml --node-ip <LAN IP>` | `0.0.0.0:7880`/tcp, `7881`/tcp, UDP `50000-60000` | `livekit` (host 네트워크 오버레이) |
| `ev211-api` | `venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1` | `127.0.0.1:8000` (루프백 전용) | `field-api` |
| `ev211-caddy` | `caddy.exe run --config app\Caddyfile` | `:80`, `:8443`, `127.0.0.1:8880` | `caddy` |

시작 순서와 의존성은 `livekit → api → caddy` 입니다(서비스 의존성으로 등록). 비정상 종료 시 5초/10초/30초 후 자동 재시작하고 1시간 뒤 실패 횟수를 초기화합니다(도커 `restart: unless-stopped` 대응). 모든 서비스는 부팅 시 **지연 자동 시작**으로, 네트워크(DHCP 예약 IP)가 준비된 뒤 기동합니다.

도커판과 동작을 맞춘 부분:

- **LiveKit 설정**: 저장소 루트 `livekit.yaml` 을 템플릿으로 그대로 쓰고 `scripts/livekit-entrypoint.sh` 처럼 `${LIVEKIT_API_KEY}`·`${LIVEKIT_WEBHOOK_URL}` 을 치환합니다. 웹훅은 Linux host 오버레이와 같은 `http://localhost:8880/api/livekit/webhook` (Caddy 루프백 경유, XFF=127.0.0.1 고정)입니다. UDP 범위는 `ev211.env` 의 `FIELD_UDP_START/END`(기본 50000-60000)로 렌더링합니다.
- **field-api 환경변수**: `docker-compose.yml`/`docker-compose.host.yml` 과 같은 이름·의미 — `FIELD_DB_PATH`, `FIELD_RECORDINGS_PATH`, `LIVEKIT_HOST=http://127.0.0.1:7880`, `LIVEKIT_RTC_URL=ws://127.0.0.1:7880`, `FIELD_WS_URL`(기본 `ws://<LAN IP>:7880`), 비밀번호·키 등.
- **Caddy**: [`Caddyfile.windows`](Caddyfile.windows) 는 루트 `Caddyfile` 과 같은 보안 동작(보안 헤더, no-cache, `default_sni`, `:80` 리다이렉트, 8443 `X-Forwarded-Proto https`, 8880 `XFF=127.0.0.1` 고정)을 유지하고 업스트림만 `127.0.0.1:7880`/`127.0.0.1:8000` 으로 바꿨습니다. 도커판이 포트 publish 로 보장하던 "8880 은 루프백 전용" 을 `bind 127.0.0.1` 로 명시합니다.

도커판과 다른 부분(의도적):

| 항목 | 도커판 | Windows 네이티브 | 이유 |
|---|---|---|---|
| LiveKit 키 주입 | `LIVEKIT_KEYS` 환경변수 | 렌더링된 `config\livekit.yaml` 의 `keys:` (관리자·SYSTEM 전용 ACL) | 서비스 XML 에 비밀을 두지 않기 위함. `--key-file` 은 Go 의 POSIX 권한 검사(0600) 때문에 Windows 에서 사용 불가 |
| field-api 비밀 로딩 | compose 가 env 주입 | `uvicorn --env-file config\ev211.env` (python-dotenv, 잠금 파일에 포함) | 같은 이유. 경로·업스트림 같은 비밀 아닌 값은 XML 이 주입하며 env 파일보다 우선 |
| 신뢰 프록시 | 사설 대역 전체 + 127.0.0.1 | `127.0.0.1` 만 | 프록시(Caddy)가 같은 호스트 루프백에서만 접속 |
| Caddy 루트 CA 신뢰 저장소 설치 | 컨테이너라 실패(무해) | `skip_install_trust` 로 명시적 생략 | SYSTEM 서비스가 서버 PC 신뢰 저장소를 바꾸지 않게(도커판과 같은 결과) |
| 데이터 위치 | named volume | `C:\ProgramData\EV211Field\{data,recordings}` | — |

## 서비스 래퍼: WinSW 를 선택한 이유

[WinSW](https://github.com/winsw/winsw) v2.12.0 을 씁니다. NSSM 과 비교한 근거:

- **설정이 XML 파일**입니다. `install.ps1`/`ev211ctl.ps1 restart` 가 `ev211.env` 에서 XML 을 생성하므로 설정이 코드로 재현되고 diff·검토가 가능합니다. NSSM 은 설정을 레지스트리(`nssm set ...`)에 저장해 재현·검토가 어렵습니다.
- **유지보수 상태**: WinSW 는 GitHub 에서 릴리스·이슈가 관리됩니다. NSSM 은 마지막 정식 릴리스가 2.24(2014), 프리릴리스 2.24-101(2017)이고 배포 사이트가 자주 불안정합니다.
- 의존성(`<depend>`), 실패 시 재시작(`<onfailure>`), 지연 자동 시작, 로그 크기 회전(`roll-by-size`)을 XML 에 선언할 수 있습니다.
- `WinSW-x64.exe` 는 자체 포함(self-contained) 단일 실행 파일이라 .NET 런타임 설치가 필요 없습니다.

참고: v3 는 아직 알파라 안정판 v2.12.0 에 고정했습니다. WinSW 는 XML 을 **서비스 시작 때마다** 다시 읽으므로 `ev211ctl restart` 로 설정 변경이 반영됩니다(의존성·시작 유형만 등록 시점에 고정 → `install.ps1` 재실행으로 반영).

## 사전 준비

### 1. Windows

- Windows 10 1809 이상 또는 Windows 11, **x64**(ARM64 미검증). 기본 포함 `curl.exe`·`tar.exe` 를 사용합니다.
- 관리자 계정. 설치·운영 명령은 모두 **관리자 권한 PowerShell** 에서 실행합니다.
- 유선 LAN 권장. 공유기에서 **DHCP 예약(고정 IP)** 을 설정하세요.

### 2. Python 3.12 (x64)

- [python.org](https://www.python.org/downloads/windows/) 의 **Python 3.12.x Windows installer (64-bit)** 를 받아 설치합니다(`requirements.lock` 은 cp312 휠 기준이라 3.13 이상·32비트 불가).
- 설치 화면에서 **Customize installation → "Install Python 3.12 for all users"** 를 선택하세요(`C:\Program Files\Python312`). 서비스는 SYSTEM 계정으로 실행되므로 사용자 프로필 아래 설치나 **Microsoft Store 판은 쓰지 마세요**.
- 설치 파일 서명은 탐색기 → 속성 → 디지털 서명(Python Software Foundation)으로 확인합니다.
- `requirements.lock` 의 모든 패키지는 `win_amd64`/`py3-none-any` 휠 해시를 포함합니다(2026-11-05 PyPI 대조 확인, `livekit==1.1.13` 은 `py3-none-win_amd64` 휠). 그래서 Dockerfile 과 같은 `pip install --require-hashes -r requirements.lock` 을 그대로 씁니다. `uvloop` 는 `sys_platform != 'win32'` 마커로 자동 제외됩니다.

### 3. 바이너리 고정 버전

`install.ps1` 은 아래 파일을 **체크섬으로 검증한 뒤에만** 설치합니다. 버전은 `docker-compose.yml` 의 이미지 버전과 같습니다. 고정값은 [`ev211-common.ps1`](ev211-common.ps1) 의 `$script:EV211Pins` 에 있습니다.

| 구성 요소 | 파일 | 받는 곳 | 체크섬 출처 |
|---|---|---|---|
| LiveKit 1.13.3 | `livekit_1.13.3_windows_amd64.zip` | [GitHub 릴리스 v1.13.3](https://github.com/livekit/livekit/releases/tag/v1.13.3) | 같은 릴리스의 `checksums.txt` (SHA256 `d29786e6…f029727`) |
| Caddy 2.11.4 | `caddy_2.11.4_windows_amd64.zip` | [GitHub 릴리스 v2.11.4](https://github.com/caddyserver/caddy/releases/tag/v2.11.4) | 같은 릴리스의 `caddy_2.11.4_checksums.txt` (SHA512 `cd5ccfd8…4788e35`) |
| WinSW 2.12.0 | `WinSW-x64.exe` | [GitHub 릴리스 v2.12.0](https://github.com/winsw/winsw/releases/tag/v2.12.0) | 공식 체크섬 파일 없음 → 2026-11-05 릴리스 자산을 직접 해시 (SHA256 `05b82d46…b3a0da`) |
| LAME 3.100 | `lame\lame.exe` (+ 동봉 DLL) | 아래 "lame 준비" | 고정 해시 없음(설치 시 SHA256 을 출력·기록) |

`-Download` 를 붙이면 LiveKit·Caddy·WinSW 를 위 URL 에서 받아 `windows\downloads\` 에 저장한 뒤 같은 방식으로 검증합니다. 인터넷이 없는 현장 PC 는 다른 PC 에서 받은 파일을 `windows\downloads\` 에 복사하세요(`downloads\` 는 Git 에서 제외).

#### lame 준비

공식 LAME 프로젝트는 Windows 바이너리를 배포하지 않습니다. 둘 중 하나를 고르세요.

1. **MSYS2 패키지(서명 검증, 권장)**: [MSYS2](https://www.msys2.org/) 에서 `pacman -S mingw-w64-ucrt-x86_64-lame` → `C:\msys64\ucrt64\bin\` 의 `lame.exe` 와 의존 DLL(`libmp3lame-0.dll` 등, `ldd lame.exe` 로 확인)을 `windows\downloads\lame\` 에 복사. pacman 이 패키지 서명을 검증합니다.
2. **RareWares LAME 3.100 x64 번들**: [rarewares.org](https://www.rarewares.org/mp3-lame-bundle.php) 의 x64 번들 압축을 풀어 `lame.exe`(필요 시 `lame_enc.dll`)를 `windows\downloads\lame\` 에 둡니다. 서명·체크섬이 없으므로 받은 파일을 백신으로 검사하세요.

field-api 는 `shutil.which("lame")` 로 인코더를 찾으며, 서비스 XML 이 `bin\lame` 을 `PATH` 앞에 넣습니다. 확인: `bin\lame\lame.exe --version`.

## 설치

관리자 PowerShell 에서 저장소 루트로 이동한 뒤:

```powershell
# 저장소를 받는다(ZIP 다운로드 후 압축 해제해도 된다)
git clone https://github.com/babcider/ev211-field-server.git C:\src\ev211-field-server
cd C:\src\ev211-field-server

# (lame 을 windows\downloads\lame\ 에 둔 뒤) 설치
powershell -NoProfile -ExecutionPolicy Bypass -File .\windows\install.ps1 -Download
```

주요 옵션:

| 옵션 | 설명 |
|---|---|
| `-Root <경로>` | 설치 루트(기본 `C:\ProgramData\EV211Field`, 환경변수 `EV211_ROOT` 로도 지정) |
| `-DownloadDir <경로>` / `-Download` | 원본 파일 폴더 / GitHub 에서 자동 다운로드 |
| `-LameDir <경로>` | lame 폴더(기본 `<DownloadDir>\lame`) |
| `-PythonExe <경로>` | Python 3.12 x64 경로(기본: `py -3.12` → `python.exe`) |
| `-NodeIp <IP>` | LAN IP 수동 지정(자동 감지가 틀릴 때, 기존 설치면 IP 만 갱신) |
| `-UdpStart/-UdpEnd` | RTC UDP 범위(최초 설치 때만 기록, 기본 50000/60000) |
| `-ConfigurePower` | AC 전원에서 절전·최대 절전·디스크 끄기 해제 |
| `-RecreateVenv`, `-SkipPortCheck`, `-SkipFirewall` | venv 재생성 / 포트 충돌 검사 생략 / 방화벽 규칙 생략 |

설치 스크립트가 하는 일 (`scripts/install.sh` 대응):

1. 관리자·x64·Python 3.12 x64 확인, 바이너리 체크섬 검증(실패하면 아무것도 바꾸지 않고 중단)
2. `C:\ProgramData\EV211Field\` 생성 후 **Administrators + SYSTEM 전용 ACL**(상속 차단, SID 기반이라 한국어 Windows 에서도 동작)
3. 80·8443·8880·7880·7881·8000 포트 충돌 검사(IIS·http.sys 가 80 을 쓰는 경우가 흔함)
4. 바이너리·앱(`api\app`, `web`, `livekit.yaml`, `Caddyfile.windows`, 운영 스크립트) 복사
5. `config\ev211.env` 가 **없을 때만** LAN IP 감지 + LiveKit 키/시크릿·송신/관리자 비밀번호 생성(`install.sh` 와 같은 형식). 있으면 그대로 유지 — **재실행해도 비밀은 바뀌지 않습니다**
6. venv 생성 + `pip install --require-hashes -r requirements.lock`, `import livekit.rtc, app.main` 으로 네이티브 FFI 로딩 확인
7. `config\livekit.yaml` 렌더링, WinSW 서비스 3개 등록(기존 등록은 삭제 후 재등록)
8. 방화벽 인바운드 허용(**개인(Private) 프로필만**): TCP 80, 8443, 7880, 7881 / UDP 50000-60000
9. 서비스 시작 → `ev211ctl doctor`

접속 정보 확인:

```powershell
C:\ProgramData\EV211Field\ev211ctl.cmd credentials
C:\ProgramData\EV211Field\ev211ctl.cmd doctor
```

### 설치 후 레이아웃

```
C:\ProgramData\EV211Field\          (Administrators + SYSTEM 전용)
  ev211ctl.cmd                      운영 명령 래퍼
  bin\                              livekit-server.exe, caddy.exe, WinSW-x64.exe, lame\
  app\                              api\app, web\, livekit.template.yaml, Caddyfile, windows\*.ps1
  venv\                             Python 3.12 가상환경
  config\  ev211.env                비밀·설정(단일 소스)
           livekit.yaml             렌더링본(keys 포함)
           binaries.txt             설치된 바이너리 버전·해시 기록
  services\                         ev211-*.exe(WinSW 사본) + ev211-*.xml(생성물)
  data\    field.db                 SQLite 원장
           caddy-data\, caddy-config\   Caddy internal CA·인증서(XDG_DATA_HOME/XDG_CONFIG_HOME)
  recordings\                       MP3 녹음
  logs\                             ev211-<서비스>.{out,err,wrapper}.log (10MB×8 회전)
  backups\                          ev211ctl backup 기본 위치
```

## 포트

| 포트 | 바인딩 | 방화벽 | 용도 |
|---|---|---|---|
| TCP 80 | 전체 | 개인 프로필 허용 | `https://<IP>:8443` 리다이렉트 전용 |
| TCP 8443 | 전체 | 개인 프로필 허용 | 자체서명 HTTPS 웹·`/api`·`/rtc`(wss) |
| TCP 8880 | **127.0.0.1** | 열지 않음 | 서버 PC 자신용 http(getUserMedia), LiveKit 웹훅 경로 |
| TCP 8000 | **127.0.0.1** | 열지 않음 | field-api(Caddy 뒤 전용) |
| TCP 7880 | 전체 | 개인 프로필 허용 | LiveKit signaling(모바일 앱 `ws://<IP>:7880` 직결) |
| TCP 7881 | 전체 | 개인 프로필 허용 | LiveKit TCP 미디어 폴백 |
| UDP 50000-60000 | 전체 | 개인 프로필 허용 | LiveKit RTC 미디어 |

네트워크가 **공용(Public)** 으로 분류돼 있으면 규칙이 적용되지 않습니다. `doctor` 가 이를 잡아내며, 다음으로 바꿉니다.

```powershell
Get-NetConnectionProfile
Set-NetConnectionProfile -InterfaceIndex <번호> -NetworkCategory Private
```

## 운영 명령

`C:\ProgramData\EV211Field\ev211ctl.cmd <명령>` (관리자 권한). Linux `ev211ctl` 과 같은 이름을 씁니다.

| 명령 | 설명 |
|---|---|
| `status` | 서비스 3개 상태·PID |
| `start` / `stop` | 순서대로 시작 / 역순 중지 |
| `restart` | 중지 → `ev211.env` 로 `livekit.yaml`·서비스 XML 재생성 → 시작 (env 변경 반영) |
| `doctor` | 서비스, IP 일치, 네트워크 프로필, 방화벽, lame, UDP 범위, HTTP 헬스체크(7880, 8880, 8443, LAN IP:8443) |
| `logs [livekit\|api\|caddy] [-Tail N] [-Follow]` | 로그 보기(`-Follow` 는 서비스 1개 지정) |
| `signal-logs [N]` | 최근 송수신 신호 이벤트 N건(기본 200, Linux 판과 같은 표 형식, 읽기 전용) |
| `credentials` | 서버 IP·송신/관리자 비밀번호 |
| `detect-ip [-Write]` | LAN IP 감지(`-Write` 는 `FIELD_NODE_IP`·`FIELD_WS_URL` 갱신, 이후 `restart`) |
| `export-ca [경로]` | Caddy 루트 CA 공개 인증서(`field-root-ca.crt`) 내보내기 — 개인키는 복사하지 않음 |
| `backup [폴더]` | 서비스 중지 → 백업 → 재시작. Linux 판과 같은 구조(`ev211.env` + `api-data/caddy-data/caddy-config.tar.gz`) |
| `restore <백업.tar.gz> -Yes` | 경로 검사 후 복구 → 설정 재렌더링 → `doctor` |
| `update` | **미구현(TODO)** — 아래 수동 절차 |

### IP 가 바뀌었을 때

```powershell
C:\ProgramData\EV211Field\ev211ctl.cmd detect-ip -Write
C:\ProgramData\EV211Field\ev211ctl.cmd restart
```

인증서 대상(`default_sni`·8443 사이트)도 새 IP 로 다시 발급됩니다. 단말에 설치한 루트 CA 는 그대로 유효합니다.

### 업데이트(수동)

Linux `ev211ctl update`(백업 → `git pull --ff-only` → 재기동 → 실패 시 롤백)의 Windows 판은 아직 없습니다.

```powershell
C:\ProgramData\EV211Field\ev211ctl.cmd backup
cd C:\src\ev211-field-server
git pull --ff-only
powershell -NoProfile -ExecutionPolicy Bypass -File .\windows\install.ps1
```

`install.ps1` 재실행은 기존 `ev211.env`(비밀)·DB·녹음·인증서를 유지하고 앱·바이너리·venv 의존성·서비스 정의만 갱신합니다. 문제가 생기면 이전 커밋으로 `git checkout` 후 다시 실행하고 필요하면 `restore` 합니다.

## 제거

```powershell
# 서비스·방화벽 규칙·실행 파일 삭제, 데이터(config·data·recordings·logs·backups)는 보존
powershell -NoProfile -ExecutionPolicy Bypass -File C:\ProgramData\EV211Field\app\windows\uninstall.ps1

# 전체 삭제(비밀번호·DB·녹음·CA 포함, 복구 불가) — 먼저 backup 하세요
powershell -NoProfile -ExecutionPolicy Bypass -File C:\ProgramData\EV211Field\app\windows\uninstall.ps1 -PurgeData
```

## 보안 메모

- **설치 루트 전체가 Administrators + SYSTEM 전용**입니다(일반 사용자 읽기 불가). 비밀은 `config\ev211.env` 와 렌더링된 `config\livekit.yaml` 두 곳에만 있고, 서비스 XML·로그에는 넣지 않습니다. 백업 아카이브에도 비밀이 들어 있으니 기본 위치(`backups\`) 밖으로 옮길 때 보관에 주의하세요.
- **field-api(127.0.0.1:8000)는 같은 PC 의 모든 프로세스가 접근 가능**합니다. 도커판은 field-api 를 publish 하지 않아 호스트 프로세스가 `X-Forwarded-*` 를 위조할 수 없었지만(codex 20차), 네이티브에서는 컨테이너 격리가 없어 로컬 프로세스가 https 강제·실패 잠금·율제한을 우회할 수 있습니다. 서버 PC 에는 **신뢰하지 않는 사용자 계정·프로그램을 두지 마세요**(현장 전용 PC). 원격 데스크톱·파일 공유도 끄는 것을 권장합니다.
- 8880 은 `bind 127.0.0.1` 로 루프백에서만 열리고 방화벽에서도 열지 않습니다. 외부 단말 경로는 8443(HTTPS)뿐입니다.
- 방화벽 규칙은 **개인 프로필 전용**입니다. 공용 Wi-Fi·인터넷에 직접 연결하거나 공유기 포트 포워딩을 하지 마세요.
- 서비스는 PoC 단순화를 위해 **LocalSystem** 으로 실행됩니다. 강화 TODO: 가상 서비스 계정(`NT SERVICE\ev211-*`) 또는 전용 저권한 계정 + 폴더별 최소 권한.
- Caddy 루트 CA 개인키는 `data\caddy-data\caddy\pki\authorities\local\root.key` 에 있습니다. `export-ca` 는 공개 인증서만 내보냅니다.

## 알려진 제약·리스크

- **LiveKit 은 프로덕션에 Linux 를 권장**합니다. Windows 빌드는 공식 배포되지만 사용 사례가 적고, 이 키트는 실기 부하 검증 전입니다. **행사 전 반드시 같은 PC·같은 네트워크에서 부하 테스트**(채널 수 × 청취자 수, 장시간)를 하세요. `scripts/loadtest.sh` 는 bash 라 다른 Linux/macOS PC 에서 이 서버로 실행합니다.
- **절전 해제 필수**: 절전·최대 절전·USB 선택적 절전·NIC 전원 관리가 켜져 있으면 행사 중 끊깁니다. `-ConfigurePower` 또는 제어판에서 해제하고, 장치 관리자 → 네트워크 어댑터 → 전원 관리 → "전원을 절약하기 위해 컴퓨터가 이 장치를 끌 수 있음" 을 해제하세요.
- **Windows 업데이트 자동 재시작**: 행사 기간에는 "업데이트 일시 중지" 와 사용 시간(active hours)을 설정하세요.
- **고정 LAN IP 필수**: `--node-ip`·인증서·`FIELD_WS_URL` 이 IP 에 묶입니다. DHCP 예약을 설정하고, 바뀌면 `detect-ip -Write` → `restart`.
- **UDP 범위와 Windows 동적 포트**: Windows 기본 UDP 동적 포트(49152-65535)가 50000-60000 과 겹칩니다. LiveKit(pion)은 이미 쓰는 포트를 건너뛰므로 보통 문제없지만, Hyper-V/WSL/Docker Desktop 이 큰 범위를 예약(`netsh int ipv4 show excludedportrange protocol=udp`)하면 가용 포트가 줄어듭니다. ICE 실패가 보이면 최초 설치 전 `-UdpStart 40000 -UdpEnd 45000` 을 쓰거나 `ev211.env` 를 고친 뒤 `install.ps1` 을 재실행(방화벽 규칙 갱신)하세요.
- **`--workers 1` 고정**: SQLite 단일 연결·메모리 상태 공유 때문이며, Windows 에서는 uvicorn 이 `workers > 1` 일 때 SelectorEventLoop 로 바꿔 asyncio 서브프로세스(lame 녹음)가 동작하지 않습니다.
- 백신(Defender 포함)이 `recordings\` 실시간 검사로 I/O 를 늘리거나 WinSW·lame 을 오탐할 수 있습니다. 필요하면 설치 루트를 검사 예외로 두세요.
- WinSW 는 래핑한 프로세스가 **종료 코드 0** 으로 스스로 끝나면 실패로 보지 않아 재시작하지 않을 수 있습니다(정상 운영 중에는 해당 없음).
- `update` 자동화, 서비스 계정 강화, ARM64 는 범위 밖입니다.

## 실기 검증 체크리스트

macOS 에서 확인한 것: PowerShell 구문 파싱(pwsh 7), env 파싱·`livekit.yaml` 렌더링·WinSW XML 생성 스모크 테스트, 렌더링한 `livekit.yaml` 로 livekit-server 1.13.3 기동, `Caddyfile.windows` 의 `caddy validate`/`adapt`(8880 이 `127.0.0.1:8880` 에만 리슨, 업스트림 `127.0.0.1`), 잠금 파일의 Windows 휠 해시 존재. **아래는 실제 Windows 에서만 확인 가능합니다.**

- [ ] Windows PowerShell **5.1** 에서 `install.ps1` 전체 실행(새 PC, 재실행 두 번 — 비밀 불변 확인)
- [ ] 한국어 Windows 에서 스크립트 한글 출력·ACL(SID) 적용, `icacls C:\ProgramData\EV211Field` 결과
- [ ] `pip install --require-hashes` 성공, `import livekit.rtc` (네이티브 FFI DLL) 로딩
- [ ] WinSW: 서비스 등록·지연 자동 시작·의존성·재부팅 후 자동 기동, 프로세스 강제 종료 시 재시작
- [ ] `doctor` 전 항목 통과, 다른 노트북에서 `https://<IP>:8443` 접속(루트 CA 설치 후), 모바일 앱 `ws://<IP>:7880` 접속
- [ ] 송신·수신 오디오, 웹훅 수신(`signal-logs` 에 join/leave 기록), 녹음 MP3 생성(lame 서브프로세스)
- [ ] AI 통역 채널(OPENAI_API_KEY 설정 시)
- [ ] LAN 단말이 `http://<IP>:8880` 에 접속 **불가**한지(루프백 바인딩) 확인
- [ ] `backup` → `restore` 왕복, Linux 백업을 Windows 에 `restore`(호환성)
- [ ] `uninstall.ps1` 후 재설치 시 데이터 유지, `-PurgeData` 완전 삭제
- [ ] 부하 테스트(목표 채널·청취자 수) + 장시간(8시간) 안정성, CPU·메모리·UDP 포트 사용량
