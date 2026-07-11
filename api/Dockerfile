# EV211 field-api 컨테이너 이미지 — FastAPI + uvicorn, /data 볼륨에 SQLite 원장 저장
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

WORKDIR /app

# 원격 PCM 오디오를 채널별 MP3로 인코딩한다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends lame \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 설치(레이어 캐시).
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# 앱 소스 복사.
COPY app ./app

# SQLite 원장과 MP3 녹음 파일 볼륨.
VOLUME ["/data"]
ENV FIELD_DB_PATH=/data/field.db
ENV FIELD_RECORDINGS_PATH=/data/recordings

EXPOSE 8000

# 프로덕션은 uvicorn 단일 워커(SQLite 단일 연결·메모리 상태 공유를 위해 워커 1개 고정).
# --forwarded-allow-ips: Caddy/도커 브리지 뒤에서 X-Forwarded-For/Proto 를 신뢰해
# request.client.host·scheme 을 실제 프록시 IP 로 채운다(rate limit·https 강제 정확도).
# 도커 브리지 서브넷 전체를 신뢰(내부망 단일 노드 전제). 값은 FORWARDED_ALLOW_IPS 로 오버라이드.
ENV FORWARDED_ALLOW_IPS="10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1"
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\""]
