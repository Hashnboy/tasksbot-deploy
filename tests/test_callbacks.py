import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app import callbacks


def test_mk_and_parse_roundtrip():
    data = callbacks.mk_cb("test", id=1)
    parsed = callbacks.parse_cb(data)
    assert parsed is not None
    assert parsed["a"] == "test"
    assert parsed["id"] == 1


def test_parse_cb_bad_signature():
    bad = "deadbe|{" "a" ": " "x" "}"
    assert callbacks.parse_cb(bad) is None


def test_validate_callback():
    cb = callbacks.mk_cb("ping", foo="bar")
    res = callbacks.validate_callback(cb)
    assert res.ok and res.payload["foo"] == "bar"
