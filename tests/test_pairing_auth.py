"""HTTP pairing tests for viosc (e42s01).

Covers the PairingGate unit (token issue/verify/expiry, peer binding, the
disabled case) and its enforcement on the :8686 HTTP data plane: POST /auth
returns a bearer token for the right code, every other request without a valid
token answers 401, and with pairing off nothing changes.

External boundaries are monkeypatched as in test_preview_http; the HTTP server
binds an ephemeral port and is driven with urllib.
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pairing
import preview_http


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_gate(*, enabled=True, code="1234", ttl=60, lease=300, trusted=None, clock=None):
    clock = clock or FakeClock()
    registry = pairing.PeerRegistry(lease_seconds=lease, trusted=trusted, clock=clock)
    gate = pairing.PairingGate(
        enabled=enabled, code=code, registry=registry, token_ttl=ttl, clock=clock
    )
    return gate, registry, clock


# ---------------------------------------------------------------------------
# e42s01t3 — RED: the PairingGate unit
# ---------------------------------------------------------------------------


def test_gate_authenticate_returns_a_token_and_binds_the_peer():
    gate, _registry, _clock = make_gate()
    token = gate.authenticate("1234", "10.0.0.5")
    assert token
    assert gate.verify(token) is True
    assert gate.is_bound("10.0.0.5") is True


def test_gate_rejects_a_wrong_code_without_binding():
    gate, _registry, _clock = make_gate()
    assert gate.authenticate("9999", "10.0.0.5") is None
    assert gate.is_bound("10.0.0.5") is False


def test_gate_token_expires_after_its_ttl():
    gate, _registry, clock = make_gate(ttl=60)
    token = gate.authenticate("1234", "10.0.0.5")
    clock.advance(61)
    assert gate.verify(token) is False


def test_gate_disabled_allows_everything():
    gate, _registry, _clock = make_gate(enabled=False)
    assert gate.verify("whatever") is True
    assert gate.is_bound("10.0.0.5") is True


def test_gate_osc_peer_authentication_binds_without_a_token():
    gate, _registry, _clock = make_gate()
    assert gate.authenticate_peer("1234", "10.0.0.5") is True
    assert gate.is_bound("10.0.0.5") is True
    assert gate.authenticate_peer("0000", "10.0.0.6") is False
    assert gate.is_bound("10.0.0.6") is False


# ---------------------------------------------------------------------------
# e42s01t3 — RED: HTTP enforcement (POST /auth + 401)
# ---------------------------------------------------------------------------


class AuthServerFixture:
    def __init__(self, gate):
        self.httpd = preview_http.start_preview_server(
            "127.0.0.1",
            0,
            {},
            lambda p: None,
            None,
            lambda: json.dumps({"current_source": None, "sources": {}, "monitored": {}}),
            gate,
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def request(self, method, path, headers=None, body=None):
        req = urllib.request.Request(
            self.base + path, headers=headers or {}, method=method, data=body
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _auth_body(code):
    return json.dumps({"code": code}).encode("utf-8")


def test_http_without_a_token_is_unauthorized():
    gate, _registry, _clock = make_gate()
    srv = AuthServerFixture(gate)
    try:
        status, _headers, _body = srv.request("GET", "/state")
        assert status == 401
    finally:
        srv.close()


def test_http_post_auth_with_the_right_code_returns_a_token_and_authorizes():
    gate, _registry, _clock = make_gate()
    srv = AuthServerFixture(gate)
    try:
        status, _headers, body = srv.request("POST", "/auth", body=_auth_body("1234"))
        assert status == 200
        token = json.loads(body)["token"]
        status, _headers, _body = srv.request(
            "GET", "/state", headers={"Authorization": f"Bearer {token}"}
        )
        assert status == 200
    finally:
        srv.close()


def test_http_post_auth_with_a_wrong_code_is_unauthorized():
    gate, _registry, _clock = make_gate()
    srv = AuthServerFixture(gate)
    try:
        status, _headers, _body = srv.request("POST", "/auth", body=_auth_body("0000"))
        assert status == 401
    finally:
        srv.close()


def test_http_with_a_bogus_token_is_unauthorized():
    gate, _registry, _clock = make_gate()
    srv = AuthServerFixture(gate)
    try:
        status, _headers, _body = srv.request(
            "GET", "/state", headers={"Authorization": "Bearer not-a-token"}
        )
        assert status == 401
    finally:
        srv.close()


def test_http_pairing_disabled_leaves_the_old_behaviour():
    gate, _registry, _clock = make_gate(enabled=False)
    srv = AuthServerFixture(gate)
    try:
        status, _headers, _body = srv.request("GET", "/state")
        assert status == 200
    finally:
        srv.close()
