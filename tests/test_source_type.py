"""The source `type` over OSC (e68s01).

vimix commit 10abe99d answers `/vimix/<t>/get s type` with an integer code (the wiki's
family/code table). viOSC must ASK for it — vimix pushes neither `uri` nor `type` — and carry
it in the state table VJmix reads. These tests pin the request, the ingestion and the payload.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import viosc


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    monkeypatch.setattr(viosc, "request_source_props", lambda *a, **kw: None)
    yield
    viosc.vimix_data.clear()
    viosc.known_indices.clear()


def test_type_is_a_supported_property():
    assert "type" in viosc.ALL_PROPERTIES, "the wiki documents it; viOSC must accept it"
    assert set(viosc.ALL_PROPERTIES) == viosc.SUPPORTED_PROPERTIES


def test_the_first_sighting_asks_for_uri_and_type(monkeypatch):
    asked: list[tuple] = []
    monkeypatch.setattr(
        viosc, "request_source_props", lambda target, props: asked.append((target, props))
    )

    viosc.process_vimix_message("/vimix/5/name", ["shader"])

    assert asked == [("shader", ["uri", "type"])], "ONE get with both discovery attributes"


def test_a_rename_asks_again(monkeypatch):
    asked: list[tuple] = []
    monkeypatch.setattr(
        viosc, "request_source_props", lambda target, props: asked.append((target, props))
    )

    viosc.process_vimix_message("/vimix/5/name", ["clipA"])
    viosc.process_vimix_message("/vimix/5/name", ["clipB"])

    assert [t for t, _p in asked] == ["clipA", "clipB"]


def test_the_type_answer_lands_in_the_entry():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])
    viosc.process_vimix_message("/vimix/5/type", [27])

    assert viosc.vimix_data[5]["type"] == 27, "the Text code survives the ingest"


def test_the_empty_entry_carries_the_key():
    assert "type" in viosc.create_empty_vimix_entry(), "absent until answered, never a KeyError"


def test_the_state_payload_carries_the_type():
    entry = viosc.create_empty_vimix_entry()
    entry["name"] = "a text"
    entry["type"] = 27
    viosc.vimix_data[0] = entry

    assert viosc.state_payload()["sources"][0]["type"] == 27


def test_an_unlisted_property_is_still_ignored():
    assert viosc.process_vimix_message("/vimix/5/nonsense", [1]) is None
    assert "nonsense" not in viosc.vimix_data.get(5, {})
