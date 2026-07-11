# EV211 Field Server

EV211 앱이 같은 Wi-Fi 안에서 음성을 송신·수신하고 무전기 채널을 사용할 수 있게 하는 자체 호스팅 서버입니다. 인터넷 연결 없이도 LiveKit 음성 중계, 채널 관리 API, 관리자 화면이 로컬 Ubuntu 서버에서 동작합니다.

이 저장소에는 로컬 서버 코드만 있습니다. EV211 클라우드, AI 번역, 결제, 데스크톱 앱, Android·iOS 앱 소스는 포함하지 않습니다. 앱의 `폰서버접속` 모드도 이 서버를 사용하지 않습니다.

## 주요 기능

- 최대 15개 음성 채널과 Floor 원어 채널을 운영합니다.
- Android·iOS 앱에서 채널 수신, 송신, PTT 무전기를 지원합니다.
- 송신·수신 바로미터로 오디오 신호를 확인할 수 있습니다.
- 관리자 웹에서 방송·무전기 채널을 모니터링하고 채널별 MP3 녹음과 관리자·송신자 비밀번호 변경을 관리합니다.
- 송신자 비밀번호와 관리자 비밀번호를 분리합니다.
- 로컬 HTTPS, 서버 인증서 TOFU 확인, 단기 LiveKit 토큰을 사용합니다.
- Docker Compose와 systemd로 자동 시작하며 백업·진단 명령을 제공합니다.

## 권장 환경

- Ubuntu Server 24.04 LTS 또는 22.04 LTS, 64비트 x86_64.
- 4코어 CPU, 메모리 8GB 이상, 여유 공간 20GB 이상.
- 가능하면 서버는 공유기에 유선으로 연결합니다.
- 모든 앱 단말과 서버가 같은 사설 네트워크에 있어야 합니다.
- 공유기의 AP 격리 또는 클라이언트 격리 기능을 꺼야 합니다.
- 공유기 DHCP 예약으로 서버 IP를 고정해야 합니다.

현재 구성은 단일 Ubuntu 서버를 전제로 합니다. 실제 수용 인원은 서버와 Wi-Fi 성능에 따라 달라지므로 행사 전 실제 단말 수로 부하를 확인하세요.

## 빠른 설치

먼저 [Docker 공식 Ubuntu 안내](https://docs.docker.com/engine/install/ubuntu/)에 따라 Docker Engine과 Docker Compose 플러그인을 설치합니다. Docker Desktop이 아니라 Ubuntu용 Docker Engine을 사용하세요.

```bash
sudo apt update
sudo apt install -y git curl openssl
sudo git clone https://github.com/babcider/ev211-field-server.git /opt/ev211-field-server
cd /opt/ev211-field-server
sudo ./scripts/install.sh
```

설치가 끝나면 다음 정보를 확인합니다.

```bash
sudo ev211ctl doctor
sudo ev211ctl credentials
```

`credentials`에 표시되는 서버 IP, 송신 비밀번호, 관리자 비밀번호를 운영 담당자에게만 전달하세요. 일반 청취자에게는 서버 IP만 안내하면 됩니다.

Docker 설치와 Ubuntu 방화벽 설정을 포함한 전체 절차는 [설치 안내](docs/INSTALL.md)를 따르세요.

## 앱에서 접속

App Store와 Google Play 배포 링크는 심사 완료 후 이 문서에 추가됩니다. 현재 앱을 설치한 단말에서는 다음 순서로 접속합니다.

1. EV211을 열고 `로컬서버접속`을 선택합니다.
2. 설치 시 표시된 서버 IP를 입력합니다. `https://`나 포트는 붙이지 않습니다.
3. 청취자는 `수신 접속`을 누르고 원하는 채널을 선택합니다.
4. 송신자는 `송신자이신가요?`를 누르고 송신 비밀번호를 입력합니다.
5. 무전기는 `무전기(인터컴) 모드`를 누르고 송신 비밀번호를 입력합니다.
6. 관리자는 화면 아래 `관리자`에서 관리자 비밀번호로 접속합니다.

처음 연결할 때 앱은 서버의 자체 서명 인증서를 기억합니다. 서버를 재설치해 인증서가 바뀌면 경고가 나타나며, 현장 관리자가 재설치 사실을 확인한 뒤에만 다시 신뢰해야 합니다.

채널 운영, 송신 바로미터, 무전기 PTT와 연속 송신, 폰서버 모드를 포함한 설명은 [Android·iOS 앱 사용 안내](docs/APP_GUIDE.md)를 참고하세요. 관리자 웹의 녹음 사용법은 [녹음 안내](docs/RECORDING.md)에 있습니다.

## 운영 명령

```bash
sudo ev211ctl status
sudo ev211ctl doctor
sudo ev211ctl logs
sudo ev211ctl restart
sudo ev211ctl backup
sudo ev211ctl update
```

설정과 자동 생성 비밀정보는 `/etc/ev211-field/ev211.env`에 저장되며 권한은 root 전용인 `600`입니다. 데이터와 Caddy 인증서는 Docker named volume에 저장됩니다. 업데이트 전에는 `ev211ctl update`가 자동으로 백업합니다.

## 네트워크 포트

| 포트 | 프로토콜 | 용도 |
|---|---|---|
| 80 | TCP | 서버 IP만 입력했을 때 HTTPS 안내로 이동 |
| 8443 | TCP | 앱 API, 관리자 웹, WSS 프록시 |
| 7880 | TCP | LiveKit signaling |
| 7881 | TCP | UDP가 막혔을 때 미디어 폴백 |
| 50000–60000 | UDP | WebRTC 음성 미디어 |

인터넷 공유기에서 포트 포워딩하지 마세요. 이 서버는 신뢰할 수 있는 내부 네트워크에서만 사용하도록 설계되었습니다. 자세한 보안 경계와 문제 해결은 [네트워크 안내](docs/NETWORK.md)와 [보안 정책](SECURITY.md)에 있습니다.

## 개발

```bash
python3 -m venv .venv
.venv/bin/pip install -r api/requirements-dev.txt
cd api
../.venv/bin/python -m pytest -q
```

API 계약은 [OpenAPI 문서](api/openapi.yaml)에 있습니다. 운영 구성은 기본 Compose 파일에 Linux host-network 오버레이를 합쳐 사용합니다.

```bash
docker compose --env-file .env \
  -f docker-compose.yml \
  -f docker-compose.host.yml config
```

브라우저용 `listen-test.html`과 `speak-test.html`, 토큰 스크립트는 개발·진단 도구이며 일반 앱 사용자는 사용할 필요가 없습니다.

## 라이선스

이 저장소의 코드는 [Apache License 2.0](LICENSE)으로 배포됩니다. 포함된 오픈소스와 Docker 이미지의 고지는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 확인하세요.

공개 전 수행한 비밀정보·의존성·컨테이너 검사는 [Public 공개 점검 결과](PUBLIC_RELEASE_AUDIT.md)에 기록돼 있습니다.
