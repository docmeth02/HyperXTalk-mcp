"""Phase 2: opaque handle encode/decode."""

import pytest

from hyperxtalk_mcp import handles

SAMPLE = {
    "v": 1,
    "stack": {
        "shortName": "Foo",
        "mainStackName": "Foo",
        "fileName": "/p/Foo.livecode",
        "stackId": 1002,
    },
    "cardId": 1002,
    "objType": "button",
    "objId": 1017,
}


def test_roundtrip():
    encoded = handles.encode(SAMPLE)
    assert isinstance(encoded, str)
    assert "\n" not in encoded and "\r" not in encoded
    assert handles.decode(encoded) == SAMPLE


def test_decode_malformed_raises():
    # valid base64 ("xyz") but not JSON
    with pytest.raises(ValueError):
        handles.decode("eHl6")
