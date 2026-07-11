# Ubuntu 설치 안내

## 설치 전 준비

Ubuntu Server 24.04 LTS를 권장합니다. 서버는 공유기에 유선으로 연결하고 공유기에서 DHCP 예약을 설정해 IP가 바뀌지 않게 하세요.

Docker는 [Docker 공식 Ubuntu 설치 안내](https://docs.docker.com/engine/install/ubuntu/)에 따라 Docker Engine과 Compose 플러그인을 설치하세요. 배포판의 비공식 `docker.io` 패키지와 Docker Desktop은 이 서버의 기준 환경이 아닙니다. 이미 설치했다면 다음 명령이 모두 성공하는지만 확인합니다.

```bash
docker --version
docker compose version
```

## 서버 설치

```bash
sudo apt update
sudo apt install -y git curl openssl
sudo git clone https://github.com/babcider/ev211-field-server.git /opt/ev211-field-server
cd /opt/ev211-field-server
sudo ./scripts/install.sh
```

설치 스크립트는 다음 작업을 수행합니다.

- 서버 LAN IP를 자동 감지합니다.
- LiveKit 키, 송신 비밀번호, 관리자 비밀번호를 무작위로 생성합니다.
- `/etc/ev211-field/ev211.env`를 root 전용 권한으로 저장합니다.
- `ev211-field.service`를 등록하고 부팅 시 자동 시작합니다.
- LiveKit, field-api, Caddy 컨테이너를 빌드하고 상태를 확인합니다.

초기 접속 정보를 확인합니다.

```bash
sudo ev211ctl credentials
sudo ev211ctl doctor
```

## 방화벽

UFW를 사용하는 서버에서는 SSH 접속을 유지할 수 있도록 SSH를 먼저 허용한 뒤 EV211 포트를 엽니다.

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 8443/tcp
sudo ufw allow 7880/tcp
sudo ufw allow 7881/tcp
sudo ufw allow 50000:60000/udp
sudo ufw enable
sudo ufw status
```

이 규칙은 서버가 신뢰할 수 있는 내부망에만 연결된다는 전제입니다. 공인 인터넷에 직접 노출하거나 공유기 포트 포워딩을 설정하지 마세요.

## 설치 확인

서버 자신에서 다음 명령이 성공해야 합니다.

```bash
curl -fsS http://localhost:7880/
curl -fsS http://localhost:8880/api/healthz
curl -kfsS https://localhost:8443/api/healthz
sudo ev211ctl status
```

그다음 Android 또는 iPhone을 같은 Wi-Fi에 연결하고 EV211의 `로컬서버접속`에서 서버 IP를 입력합니다. 서버 IP에는 프로토콜이나 포트를 붙이지 않습니다.

## 업데이트

```bash
sudo ev211ctl update
```

업데이트는 현재 데이터와 설정을 먼저 백업하고, Git 저장소를 fast-forward로 갱신한 뒤 컨테이너를 다시 빌드합니다. 저장소에 로컬 수정이 있으면 안전을 위해 중단합니다.

## 제거

다음 명령은 서비스를 중지하고 자동 시작만 해제합니다. 데이터와 비밀번호는 삭제하지 않습니다.

```bash
sudo systemctl disable --now ev211-field.service
```

완전 삭제는 `/opt/ev211-field-server`, `/etc/ev211-field`, `ev211_field_*` Docker volume을 제거하므로 반드시 백업 후 수동으로 수행하세요.
