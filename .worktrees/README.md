# 로컬 보조 Git worktree

이 디렉터리는 `ev211-field-server` 저장소에 연결된 로컬 보조 worktree를 한곳에 모아 두는 위치입니다.

- `ev211-field-server-offline-hotfix`는 기존 `../ev211-field-server-offline-hotfix`에서 이동한 `hotfix/offline-web-publish` worktree입니다.
- 오프라인 웹 시그널링과 브라우저 송신 음질 선택 변경을 보존하는 운영 핫픽스 작업본입니다.
- 하위 worktree는 로컬 전용이며 이 저장소의 커밋 대상이 아닙니다.

worktree를 옮길 때는 `.git` 연결 정보가 함께 갱신되도록 `git worktree move`를 사용합니다.
