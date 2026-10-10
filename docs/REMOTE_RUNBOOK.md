# EV211 필드서버 원격 대응 명령 모음

필드서버 PC 에 SSH 로 접속한 상태에서 쓴다. `<계정>` 은 그 PC 의 sudo 가능한 리눅스 계정이다.
`sudo` 가 붙은 명령은 그 계정의 리눅스 비밀번호를 묻는다. 나머지는 비밀번호 없이 된다.

## 0. 접속

```bash
# 한국 쪽 기기에서 (Tailscale 로그인 상태)
ssh <계정>@<서버 Tailscale 주소>

# 관리자 화면까지 보려면: 접속 후 내 브라우저에서 https://localhost:8443/admin.html
ssh -L 8443:localhost:8443 <계정>@<서버 Tailscale 주소>
```

Tailscale 주소로 관리자 화면을 직접 열면 빈 페이지가 나온다. 반드시 위 포트 포워딩을 쓴다.

## 1. 30초 상태 확인 (가장 먼저)

```bash
docker ps --format '{{.Names}}\t{{.Status}}'          # 3개 모두 Up, api·livekit 은 (healthy)
curl -s http://localhost:8880/api/healthz; echo        # {"status":"ok","livekit":"connected",...}
ip route get 1.1.1.1 | grep -o 'src [0-9.]*'           # 지금 이 PC 의 LAN IP
sudo ev211ctl credentials                              # 설정된 서버 IP + 비밀번호 (화면에 비밀번호가 나온다)
```

마지막 두 줄의 IP 가 다르면 **4번(IP 변경)** 이 원인이다. 현장에서 가장 흔한 문제다.

채널·청취자 현황:

```bash
curl -s http://localhost:8880/api/status | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('세대 G%s, 총 청취자 %s' % (d['generation'], d['total_listeners']))
for c in d['channels']:
    if c['state']=='open': print(' 채널', c['channel_id'], c['label'], '| 송신 중' if c['on_air'] else '| 대기', '| 청취', c['listeners'])"
```

## 2. 증상별 로그

### "송신이 안 된다" / "소리가 안 들린다"

```bash
# 최근 10분간 접속에 성공한 참가자와 실패한 참가자 수
docker logs --since 10m field-livekit 2>&1 | grep -c 'participant active'
docker logs --since 10m field-livekit 2>&1 | grep -c 'removing participant without connection'

# 실패한 참가자가 누구인지 (speaker=송신자, listener=청취자, monitor=관리자 화면 레벨미터)
docker logs --since 10m field-livekit 2>&1 | grep 'removing participant without connection' \
  | grep -o '"participant": "[a-z]*\|"browser": "[A-Za-z ]*"\|"os": "[A-Za-z ]*"' | paste - - - | sort | uniq -c
```

- 실패가 0 이고 성공만 있으면 서버 접속은 정상이다. 마이크·볼륨·채널 선택 등 단말 쪽을 본다.
- 특정 단말만 실패하면 그 단말의 Wi-Fi(게스트망·AP 격리)나 브라우저 문제다.
- 전부 실패하면 서버 IP 불일치(4번)나 공유기의 UDP 50000–60000 차단을 의심한다.

### "이미 송신 중인 채널입니다" (409)

```bash
# 누가 채널을 잡고 있는지 — 최근 송신자 접속/이탈 순서
docker logs --since 30m field-livekit 2>&1 \
  | grep -E 'participant active|participant closing|removing participant without connection' | grep 'speaker-ch' \
  | sed -E 's/^([0-9T:-]+)[^\t]*\t[A-Z]+\t[a-z.]+\t[^\t]+\t([a-z ]+)\t.*"participant": "(speaker-ch-[0-9]+)[^"]*".*/\1  \3  \2/' | tail -15
```

실제로 다른 사람이 송신 중이면 정상 동작이다. 관리자 화면의 "인수"로 넘겨받거나 그 송신자가 정지해야 한다.

### 비밀번호 오류 / 잠금

```bash
docker logs --since 30m field-api 2>&1 | grep -E '" (401|423|429) ' | tail -20
```

`401` 은 비밀번호 불일치, `423` 은 반복 실패로 인한 일시 잠금, `429` 는 시도 과다다. 잠금은 시간이 지나면 풀린다.

### 송수신 이력 (누가 언제 붙고 떨어졌나)

```bash
sudo ev211ctl signal-logs 100      # 최근 100건, 시각은 UTC
```

### 실시간 로그

```bash
sudo ev211ctl logs                  # 전체, Ctrl+C 로 종료
docker logs -f --tail 50 field-api  # 한 컨테이너만 (field-livekit, field-caddy 도 가능)
```

## 3. 재시작

재시작하면 모든 송신·청취가 끊긴다. 앱은 다시 접속해야 한다.

```bash
sudo ev211ctl stop       # 컨테이너를 모두 내린다
sudo ev211ctl start      # 다시 올린다
sudo ev211ctl doctor     # "진단 통과" 가 나와야 한다
```

`sudo ev211ctl restart` 는 설정이나 코드가 바뀌지 않았으면 아무것도 다시 시작하지 않는다.
멈춘 것처럼 보여 강제로 다시 띄우려면 위처럼 stop → start 를 쓴다.

PC 자체를 재부팅해도 스택은 자동으로 올라온다: `sudo reboot` (1~2분 뒤 다시 SSH 접속).

## 4. 서버 IP 가 바뀌었을 때

현장 공유기가 다른 IP 를 주면 앱·브라우저의 음성 접속이 전부 실패한다.

```bash
ip route get 1.1.1.1 | grep -o 'src [0-9.]*'      # 새 LAN IP 확인
sudo nano /etc/ev211-field/ev211.env               # FIELD_NODE_IP 와 FIELD_WS_URL 두 줄을 새 IP 로 수정
sudo ev211ctl restart                              # 설정이 바뀌었으므로 이때는 다시 뜬다
sudo ev211ctl doctor
```

고친 뒤 현장에 알릴 것:

- 앱에 입력하는 서버 IP 가 바뀐다.
- 관리자 화면 주소가 `https://<새 IP>:8443` 로 바뀌고, 인증서 경고가 다시 나올 수 있다.
- 가능하면 공유기에서 이 PC 에 DHCP 예약을 걸어 다시 바뀌지 않게 한다.

## 5. 업데이트와 되돌리기

```bash
sudo ev211ctl update     # 자동 백업 → 운영 브랜치(hotfix/offline-web-publish) 최신 반영 → 재시작
```

- 백업을 위해 전체가 한 번 재시작된다. 행사 진행 중에는 하지 않는다.
- 출력에 `Already up to date.` 가 나오면 받아 올 변경이 없었다는 뜻이다(PR 을 아직 머지하지 않은 경우).
- 백업 파일은 `/var/backups/ev211-field/` 에 쌓인다.

```bash
sudo ls -lt /var/backups/ev211-field/ | head                       # 백업 목록
sudo ev211ctl backup                                               # 수동 백업 (스택이 잠깐 멈춘다)
sudo ev211ctl restore /var/backups/ev211-field/<파일>.tar.gz --yes  # 복구 — 현재 데이터·비밀번호를 덮어쓴다
```

## 6. PC·네트워크 자체 점검

```bash
uptime                          # 부하, 켜진 시간
df -h / /var/lib/docker         # 디스크 (녹음이 쌓이면 찬다)
free -h                         # 메모리
tailscale status | head -3      # 원격 접속 경로
ping -c 3 1.1.1.1               # 현장 인터넷
```

## 하면 안 되는 것

- 운영 디렉터리(`/opt/ev211-field-server`)가 아닌 다른 사본에서 `docker compose` 를 실행하지 않는다. 컨테이너 이름과 프로젝트명이 같아 운영 스택을 덮어쓴다. 실수했다면 `sudo ev211ctl start` 로 복구한다.
- 도커 볼륨 `ev211_field_api_data` 를 지우지 않는다. 채널·비밀번호·녹음이 들어 있다.
- 볼륨 `ev211_field_caddy_data` 를 지우면 인증서가 새로 만들어져 모든 단말이 다시 신뢰 설정을 해야 한다.

## 참고 문서 (이 PC 안)

- `/opt/ev211-field-server/docs/NETWORK.md` — 포트, 공유기 설정, 증상별 확인
- `/opt/ev211-field-server/docs/BACKUP_RESTORE.md`, `RECORDING.md`, `SIGNAL_LOGS.md`
