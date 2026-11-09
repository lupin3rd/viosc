"""Frozen-OSC-surface regression guards (e41s01, rewritten for e98s04).

The frozen contract is between viOSC and Vimix, and since 1.0 its upstream is the
HTTP command stream: `POST /link` frames are the ONLY thing that becomes a
`/vimix/*` message. These characterisation tests pin that path — address, argument
order and argument types must survive the JSON round trip untouched — and they pin
the converse: nothing that arrives from Vimix is ever echoed back out.

No Vimix and no network: the frames are decoded directly and the forward client is
a recording fake.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import pairing
import viosc


class FakeUDPClient:
    """Recording stand-in for a python-osc UDP client."""

    def __init__(self, ip, port):
        self.ip = ip
        self.port = port
        self.messages = []

    def send_message(self, address, args):
        self.messages.append((address, list(args)))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(viosc, "forward_client", FakeUDPClient("127.0.0.1", 7000))
    viosc.vimix_data.clear()
    viosc.source_current = None
    yield
    viosc.vimix_data.clear()


def _frame(payload):
    viosc.apply_link_frame(json.dumps(payload).encode("utf-8"))


def test_vimix_commands_are_forwarded_verbatim():
    _frame({"address": "/vimix/clip.mp4/alpha", "args": [0.5]})

    assert viosc.forward_client.messages == [("/vimix/clip.mp4/alpha", [0.5])]


def test_vimix_multi_argument_command_keeps_its_payload_and_order():
    _frame({"address": "/vimix/clip.mp4/position", "args": [0.25, 0.75]})

    assert viosc.forward_client.messages == [("/vimix/clip.mp4/position", [0.25, 0.75])]


def test_argument_types_survive_the_json_round_trip():
    _frame({"address": "/vimix/clip.mp4/alpha", "args": [1]})
    _frame({"address": "/vimix/clip.mp4/alpha", "args": [1.0]})
    _frame({"address": "/vimix/clip.mp4/name", "args": ["a name with spaces"]})

    (first, second, third) = viosc.forward_client.messages
    assert isinstance(first[1][0], int)
    assert isinstance(second[1][0], float)
    assert third[1][0] == "a name with spaces"


def test_a_keepalive_is_not_a_command():
    _frame({"ping": True})

    assert viosc.forward_client.messages == []


def test_an_address_without_arguments_is_forwarded_as_a_trigger():
    _frame({"address": "/vimix/current/next"})

    assert viosc.forward_client.messages == [("/vimix/current/next", [])]


def test_the_vimix_side_is_never_forwarded_back(monkeypatch):
    """A bundle from Vimix is ingested: the forward lane must stay silent."""
    handler = viosc.create_osc_handler(viosc.FROMVIMIX_PORT)
    handler(("127.0.0.1", 7001), "/vimix/current/0", 1)
    handler(("127.0.0.1", 7001), "/vimix/0/alpha", 0.4)

    assert viosc.forward_client.messages == []


# ---------------------------------------------------------------------------
# BUG-2026-11-08 — the pairing gate must not starve the Vimix ingest lane
# ---------------------------------------------------------------------------


def _armed_gate(monkeypatch):
    gate = pairing.PairingGate(True, "1234", pairing.PeerRegistry(60), 300)
    monkeypatch.setattr(viosc, "PAIRING_GATE", gate)
    return gate


def test_vimix_is_heard_before_any_controller_pairs(monkeypatch):
    """viOSC talks to Vimix from the first packet, not after VJmix pairs.

    The pairing gate protects the CONTROLLER lanes. Vimix is the local OSC
    upstream, so its replies must never be dropped for lack of a pairing: the
    operator saw the mirror stay empty until VJmix connected over HTTP.
    """
    _armed_gate(monkeypatch)
    before = viosc.vimix_messages

    handler = viosc.create_osc_handler(viosc.FROMVIMIX_PORT)
    handler(("127.0.0.1", 7001), "/vimix/current/0", 1)

    assert viosc.vimix_messages == before + 1


def test_an_unpaired_peer_still_cannot_drive_the_controller_lane(monkeypatch):
    """The gate keeps its job: /viosc/* needs the code, the Vimix lane does not."""
    gate = _armed_gate(monkeypatch)
    started = []
    monkeypatch.setattr(viosc, "generate_thumbnails_worker", lambda *a: started.append(a))
    viosc.vimix_data[0] = {"uri": "/tmp/clip.mp4", "media_kind": "video"}
    handler = viosc.create_osc_handler(viosc.FROMVIMIX_PORT)

    handler(("10.0.0.9", 5000), "/viosc/regen_thumb/0")
    assert started == []

    gate.authenticate_peer("1234", "10.0.0.9")
    handler(("10.0.0.9", 5000), "/viosc/regen_thumb/0")
    assert started == [(0, "/tmp/clip.mp4")]
