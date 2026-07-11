# 백업과 복구

EV211 서버의 상태는 환경변수 파일, field-api SQLite 데이터, Caddy 로컬 CA와 설정 volume에 저장됩니다. Caddy CA를 잃으면 앱에서 인증서 변경 경고가 나타나므로 세 항목을 함께 백업해야 합니다.

## 백업

```bash
sudo ev211ctl backup
```

기본 백업 위치는 `/var/backups/ev211-field`입니다. 일관된 SQLite와 인증서 복사본을 만들기 위해 백업 중 컨테이너가 잠시 중지됐다가 다시 시작됩니다.

백업 파일을 서버 외부의 암호화된 저장소에도 복사하세요. 백업에는 송신·관리자 비밀번호와 LiveKit 키가 포함되므로 일반 파일 공유 서비스에 평문으로 올리면 안 됩니다.

## 복구

복구는 현재 설정과 데이터를 덮어씁니다. 대상 백업을 확인한 뒤 명시적으로 `--yes`를 붙입니다.

```bash
sudo ev211ctl restore /var/backups/ev211-field/ev211-field-YYYYmmdd-HHMMSS.tar.gz --yes
sudo ev211ctl doctor
```

다른 서버 장비로 복구했다면 `/etc/ev211-field/ev211.env`의 `FIELD_NODE_IP`와 `FIELD_WS_URL`이 새 장비 IP와 맞는지 확인하세요. 기존 Caddy CA도 함께 복구되므로 앱은 인증서를 다시 신뢰할 필요가 없습니다.
