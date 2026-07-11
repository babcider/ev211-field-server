# 제3자 소프트웨어 고지

이 저장소는 다음 오픈소스 소프트웨어를 사용하거나 배포 시 내려받습니다. 각 소프트웨어의 저작권과 라이선스는 해당 프로젝트에 있습니다.

| 구성 요소 | 사용 형태 | 라이선스 |
|---|---|---|
| [LiveKit Server](https://github.com/livekit/livekit) `v1.13.3` | Docker 이미지 | Apache-2.0 |
| [LiveKit JavaScript Client](https://github.com/livekit/client-sdk-js) `2.20.1` | `web/vendor/livekit-client.umd.js` 번들 | Apache-2.0 |
| [Caddy](https://github.com/caddyserver/caddy) `2.11.4` | Docker 이미지 | Apache-2.0 |
| [FastAPI](https://github.com/fastapi/fastapi) `0.139.0` | Python 패키지 | MIT |
| [Starlette](https://github.com/Kludex/starlette) `1.3.1` | Python 패키지 | BSD-3-Clause |
| [Uvicorn](https://github.com/encode/uvicorn) `0.34.0` | Python 패키지 | BSD-3-Clause |
| [LiveKit Python API](https://github.com/livekit/python-sdks) `1.1.1` | Python 패키지 | Apache-2.0 |
| [Pydantic](https://github.com/pydantic/pydantic) `2.10.4` | Python 패키지 | MIT |
| [LiveKit Python RTC](https://github.com/livekit/python-sdks) `1.1.13` | 서버 녹음 participant | Apache-2.0 |
| [LAME](https://lame.sourceforge.io/) | Debian 패키지, MP3 인코딩 | LGPL-2.0-or-later |

Docker 이미지와 Python 패키지에는 위 목록에 적지 않은 전이 의존성이 포함될 수 있습니다. 재배포자는 최종 이미지와 패키지의 라이선스 고지를 함께 확인해야 합니다.
