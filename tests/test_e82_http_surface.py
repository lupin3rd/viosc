"""The HTTP surface of the daemon (e82s03): ETag, /events (SSE), /interest,
/thumb/regen and /lane.

The delta lane is a PUSH, but the consumer owns it: one persistent connection, no
polling at rest, every byte ordered by `rev`. The transport stays dumb — the daemon
injects every collaborator, exactly like `provide_state` today (the server never
imports `viosc`: the `__main__` double-import trap).
"""

import http.client
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import preview_http


class ServerFixture:
    def __init__(
        self,
        *,
        provide_state=None,
        provide_rev=None,
        events=None,
        apply_interest=None,
        regen_thumb=None,
        apply_lane=None,
    ):
        self.httpd = preview_http.start_preview_server(
            "127.0.0.1",
            0,
            {},
            lambda path: None,
            None,
            provide_state=provide_state,
            provide_rev=provide_rev,
            events=events,
            apply_interest=apply_interest,
            regen_thumb=regen_thumb,
            apply_lane=apply_lane,
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

    def stream(self):
        """An OPEN connection to /events (the caller owns it)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/events")
        return conn, conn.getresponse()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_state_answers_304_when_the_revision_matches():
    f = ServerFixture(provide_state=lambda: '{"rev": 7}', provide_rev=lambda: 7)
    try:
        status, headers, body = f.get("/state", {"If-None-Match": '"7"'})
        assert status == 304
        assert headers["ETag"] == '"7"'
        assert body == b"", "a 304 carries no body"

        status, headers, body = f.get("/state")
        assert status == 200
        assert headers["ETag"] == '"7"'
        assert json.loads(body)["rev"] == 7
    finally:
        f.close()


def test_interest_posts_the_body_and_returns_the_report():
    seen = []
    f = ServerFixture(apply_interest=lambda payload: seen.append(payload) or {"ok": ["clipA"]})
    try:
        status, _headers, body = f.post(
            "/interest", {"source": "clipA", "props": ["alpha"], "cadence_ms": 100}
        )
        assert status == 200
        assert json.loads(body)["ok"] == ["clipA"]
        assert seen == [{"source": "clipA", "props": ["alpha"], "cadence_ms": 100}]
    finally:
        f.close()


def test_interest_problems_answer_400_with_the_report():
    f = ServerFixture(apply_interest=lambda payload: {"ok": [], "unknown": ["bogus"]})
    try:
        status, _headers, body = f.post("/interest", {"source": "clipA", "props": ["bogus"]})
        assert status == 400
        assert json.loads(body)["unknown"] == ["bogus"]
    finally:
        f.close()


def test_interest_rejects_a_malformed_body():
    f = ServerFixture(apply_interest=lambda payload: {"ok": []})
    try:
        conn = http.client.HTTPConnection("127.0.0.1", f.port, timeout=5)
        try:
            conn.request("POST", "/interest", body="not json", headers={"Content-Length": "8"})
            assert conn.getresponse().status == 400
        finally:
            conn.close()
    finally:
        f.close()


def test_events_streams_one_delta_with_its_revision():
    hub = preview_http.EventHub()
    f = ServerFixture(events=hub)
    conn, resp = f.stream()
    try:
        assert resp.status == 200
        assert resp.getheader("Content-Type").startswith("text/event-stream")
        assert resp.readline().strip() == b": connected"
        assert resp.readline().strip() == b"", "the connected frame ends with a blank line"

        hub.publish({"rev": 3, "kind": "changed", "source": "clipA", "prop": "alpha", "value": 0.4})

        assert resp.readline().strip() == b"id: 3"
        assert resp.readline().strip() == b"event: delta"
        data = json.loads(resp.readline().decode().removeprefix("data: "))
        assert data == {
            "rev": 3,
            "kind": "changed",
            "source": "clipA",
            "prop": "alpha",
            "value": 0.4,
        }
        assert resp.readline().strip() == b"", "the event ends with a blank line"
    finally:
        conn.close()
        f.close()


def test_events_keeps_the_connection_alive_with_a_heartbeat(monkeypatch):
    monkeypatch.setattr(preview_http, "SSE_HEARTBEAT_S", 0.05)
    f = ServerFixture(events=preview_http.EventHub())
    conn, resp = f.stream()
    try:
        assert resp.readline().strip() == b": connected"
        assert resp.readline().strip() == b""
        assert resp.readline().strip() == b": ping", "idle streams must not be reaped"
    finally:
        conn.close()
        f.close()


def test_the_hub_tells_a_stuck_client_to_resync():
    hub = preview_http.EventHub(maxsize=1)
    channel = hub.subscribe()
    hub.publish({"rev": 1})
    hub.publish({"rev": 2})

    assert channel.get_nowait() == {"kind": "overflow"}, (
        "a lost revision is a RESYNC, never silence"
    )
    hub.unsubscribe(channel)
    assert hub.count() == 0


def test_thumb_regen_answers_404_for_an_unknown_source():
    f = ServerFixture(regen_thumb=lambda name: False)
    try:
        status, _headers, body = f.post("/thumb/regen", {"source": "ghost"})
        assert status == 404
        assert json.loads(body)["regenerated"] is False
    finally:
        f.close()


def test_lane_is_posted_and_acked():
    seen = []
    f = ServerFixture(
        apply_lane=lambda payload: seen.append(payload) or {"lane": "pull+stream", "ttl_s": 30}
    )
    try:
        status, _headers, body = f.post(
            "/lane", {"consumer": "vjmix-1.1.0", "state": "pull+stream"}
        )
        assert status == 200
        assert json.loads(body)["ttl_s"] == 30
        assert seen[0]["consumer"] == "vjmix-1.1.0"
    finally:
        f.close()
