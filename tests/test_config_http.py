"""The config resource on the HTTP data plane (e58s01) — GET /config.

The viOSC config becomes remotely readable over the same HTTP surface that
serves /state, /thumb and /fs. The route must sit behind the pairing gate and
must return the effective config with per-field source markers, while keeping
the hidden (Vimix-local / ffmpeg) fields out of the remote-editable set.
"""

import http.client
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import http_api
import pairing
import viosc


def _effective_provider():
    values, sources, _ = config.effective({}, {})
    return {
        "values": values,
        "sources": sources,
        "editable": config.remote_editable_fields(),
    }


class ConfigServerFixture:
    def __init__(self, *, gate=None, apply_config=None, restart=None):
        self.httpd = http_api.start_http_server(
            "127.0.0.1",
            0,
            {},
            lambda path: None,
            None,
            None,
            gate,
            None,
            provide_config=_effective_provider,
            apply_config=apply_config,
            restart=restart,
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def post(self, path, payload, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            body = json.dumps(payload)
            conn.request(
                "POST",
                path,
                body=body,
                headers={**{"Content-Type": "application/json"}, **(headers or {})},
            )
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_config_is_behind_the_pairing_gate():
    gate = pairing.PairingGate(True, "1234", pairing.PeerRegistry(), 300)
    f = ConfigServerFixture(gate=gate)
    try:
        assert f.get("/config")[0] == 401
        token = gate.authenticate("1234", "127.0.0.1")
        status, _headers, body = f.get("/config", headers={"Authorization": f"Bearer {token}"})
        assert status == 200
        payload = json.loads(body)
        assert "values" in payload and "sources" in payload and "editable" in payload
    finally:
        f.close()


def test_config_does_not_leak_hidden_fields_as_editable():
    f = ConfigServerFixture(gate=None)
    try:
        status, _headers, body = f.get("/config")
        assert status == 200
        payload = json.loads(body)
        # hidden fields stay readable but are not remote-editable
        assert "ffmpeg_path" in payload["values"]
        assert "ffmpeg_path" not in payload["editable"]
        assert "tovimix_ip" not in payload["editable"]
        # a normal field is editable
        assert "sync_interval_ms" in payload["editable"]
    finally:
        f.close()


def test_config_post_requires_auth():
    gate = pairing.PairingGate(True, "1234", pairing.PeerRegistry(), 300)
    f = ConfigServerFixture(gate=gate, apply_config=viosc.apply_config_changes)
    try:
        assert f.post("/config", {"sync_interval_ms": 500})[0] == 401
    finally:
        f.close()


def test_config_post_applies_a_live_field():
    f = ConfigServerFixture(gate=None, apply_config=viosc.apply_config_changes)
    try:
        status, _headers, body = f.post("/config", {"sync_interval_ms": 500})
        assert status == 200
        payload = json.loads(body)
        assert "sync_interval_ms" in payload["applied"]
        assert viosc.sync_interval_time == 500
    finally:
        f.close()


def test_config_post_reports_invalid_and_unknown():
    f = ConfigServerFixture(gate=None, apply_config=viosc.apply_config_changes)
    try:
        status, _headers, body = f.post("/config", {"from_vimix_port": 0, "bogus": 1})
        assert status == 400
        payload = json.loads(body)
        assert "from_vimix_port" in payload["invalid"]
        assert "bogus" in payload["unknown"]
    finally:
        f.close()


def test_config_post_stages_restart_fields():
    f = ConfigServerFixture(gate=None, apply_config=viosc.apply_config_changes)
    try:
        before = viosc.FROMVIMIX_PORT
        status, _headers, body = f.post("/config", {"from_vimix_port": 7005})
        assert status == 200
        payload = json.loads(body)
        assert "from_vimix_port" in payload["staged"]
        assert before == viosc.FROMVIMIX_PORT, "restart fields apply on /restart, not now"
    finally:
        f.close()


def test_config_restart_requires_auth_and_calls_restart():
    calls = []
    gate = pairing.PairingGate(True, "1234", pairing.PeerRegistry(), 300)
    f = ConfigServerFixture(gate=gate, restart=lambda: calls.append("restarted"))
    try:
        assert f.post("/restart", {})[0] == 401
        token = gate.authenticate("1234", "127.0.0.1")
        status, _headers, _body = f.post(
            "/restart", {}, headers={"Authorization": f"Bearer {token}"}
        )
        assert status == 200
        # The handler answers FIRST and only then calls restart() (by design: the
        # client must learn that the restart is coming), so the callback lands just
        # after the response. Asserting it immediately was a race: it passed only
        # when the server thread happened to win. Wait for it, bounded.
        deadline = time.monotonic() + 2.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == ["restarted"]
    finally:
        f.close()


def test_config_post_rejects_local_only_bind():
    f = ConfigServerFixture(gate=None, apply_config=viosc.apply_config_changes)
    try:
        status, _headers, body = f.post("/config", {"http_port": 9999})
        assert status == 400
        payload = json.loads(body)
        assert "http_port" in payload["local_only"]
    finally:
        f.close()


def test_apply_config_changes_persists_live_fields(tmp_path):
    """A live change must hit the config FILE, not only the daemon globals.

    The dashboard snapshot reads the file, so a live change that was never
    persisted made viOSC keep showing the old configuration (and would vanish on
    the next restart).
    """
    cfg_path = tmp_path / "config.json"
    before = viosc.sync_interval_time
    try:
        report = viosc.apply_config_changes({"sync_interval_ms": 500}, str(cfg_path))
        assert "sync_interval_ms" in report["applied"]
        assert viosc.sync_interval_time == 500
        assert json.loads(cfg_path.read_text())["sync_interval_ms"] == 500
    finally:
        viosc.sync_interval_time = before
