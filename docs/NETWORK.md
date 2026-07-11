# 네트워크와 문제 해결

## 권장 토폴로지

Ubuntu 서버는 유선 LAN으로 공유기에 연결하고 Android·iOS 단말은 같은 공유기의 일반 Wi-Fi에 연결합니다. 게스트 Wi-Fi는 단말 간 통신을 막는 경우가 많으므로 사용하지 마세요.

공유기 설정에서 다음 항목을 확인합니다.

- 서버 MAC 주소에 DHCP 예약을 설정합니다.
- AP isolation, client isolation, 무선 단말 격리를 끕니다.
- 게스트 네트워크가 아닌 내부 네트워크를 사용합니다.
- 인터넷 방향 포트 포워딩은 설정하지 않습니다.
- 혼잡한 행사장에서는 5GHz 또는 6GHz Wi-Fi와 충분한 AP 용량을 확보합니다.

## 필요한 포트

| 방향 | 포트 | 설명 |
|---|---|---|
| 단말 → 서버 | TCP 8443 | HTTPS API와 관리자 화면 |
| 단말 → 서버 | TCP 7880 | LiveKit signaling |
| 단말 → 서버 | TCP 7881 | 미디어 TCP 폴백 |
| 단말 ↔ 서버 | UDP 50000–60000 | WebRTC 음성 |
| 단말 → 서버 | TCP 80 | HTTPS 주소 안내 |

TCP 8880은 서버 자신의 진단용 루프백 포트이므로 외부에 열면 안 됩니다.

## 증상별 확인

### 앱이 서버를 찾지 못함

서버 IP, 같은 Wi-Fi 여부, AP 격리, Ubuntu 방화벽 순서로 확인합니다. 서버에서 `ip route get 1.1.1.1`을 실행해 표시되는 `src` 주소가 앱에 입력한 IP와 같은지도 확인하세요.

### 접속은 되지만 소리가 안 들림

먼저 앱의 송신 바로미터와 수신 바로미터를 확인합니다. 송신은 움직이지만 수신이 0이면 UDP 50000–60000 차단 가능성이 큽니다. 다음 명령으로 컨테이너 상태와 서버 로그를 확인합니다.

```bash
sudo ev211ctl doctor
sudo ev211ctl logs
```

### 인증서 변경 경고

Caddy 데이터 volume을 삭제하거나 서버를 재설치하면 새 로컬 CA가 만들어집니다. 계획된 재설치가 맞는지 확인한 뒤에만 앱에서 다시 신뢰하세요. 원인을 모르면 접속을 중단합니다.

### IP가 바뀐 뒤 접속 불가

공유기 DHCP 예약을 수정하고 `/etc/ev211-field/ev211.env`의 `FIELD_NODE_IP`와 `FIELD_WS_URL`을 새 IP로 바꾼 뒤 재시작합니다.

```bash
sudo nano /etc/ev211-field/ev211.env
sudo ev211ctl restart
sudo ev211ctl doctor
```
