# 공개 브라우저 수신 화면의 필수 연결 요소를 확인하는 정적 회귀 테스트
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_public_listener_page_uses_open_subscribe_apis_and_hides_operator_login():
    page = (ROOT / "web" / "index.html").read_text()

    for text in (
        "'/channels'",
        "subscribe-tokens",
        "'/listeners/heartbeat'",
        "id=\"loginToggle\"",
        "id=\"loginPanel\" class=\"card hidden\"",
        "ev211.pendingLoginPassword",
        "new LK.Room({ autoSubscribe: false",
    ):
        assert text in page


def test_https_root_serves_public_listener_page_without_admin_redirect():
    caddyfile = (ROOT / "Caddyfile").read_text()

    assert "import web_no_cache" in caddyfile
    assert "/i18n.js" in caddyfile
    assert "redir * /admin.html" not in caddyfile


def test_linux_caddy_signaling_proxy_uses_the_server_lan_ip():
    host_overlay = (ROOT / "docker-compose.host.yml").read_text()

    assert 'LIVEKIT_UPSTREAM: "${FIELD_NODE_IP:-127.0.0.1}:7880"' in host_overlay
    assert "host.docker.internal:host-gateway" not in host_overlay.split("  caddy:", 1)[1]


def test_operator_logout_returns_to_public_listener_page():
    page = (ROOT / "web" / "admin.html").read_text()

    assert "$('logoutBtn').onclick = async () =>" in page
    assert "location.assign('/');" in page


def test_public_and_operator_pages_include_english_language_switching():
    listener = (ROOT / "web" / "index.html").read_text()
    operator = (ROOT / "web" / "admin.html").read_text()
    i18n = (ROOT / "web" / "i18n.js").read_text()

    assert 'id="languageToggle"' in listener
    assert 'id="languageToggle"' in operator
    assert "EV211 Interpretation Listener" in listener
    assert "EV211 Field Console" in operator
    assert "localStorage.getItem(storageKey)" in i18n


def test_browser_room_connections_use_the_server_advertised_lan_url():
    listener = (ROOT / "web" / "index.html").read_text()
    operator = (ROOT / "web" / "admin.html").read_text()

    assert "defaultWsUrl(grant.url)" in listener
    assert operator.count("defaultWsUrl(grant.url)") == 3
    assert "new URL(advertisedUrl)" in listener
    assert "new URL(advertisedUrl)" in operator
