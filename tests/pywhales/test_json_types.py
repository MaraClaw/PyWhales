"""Static JSON decoder contracts, checked by ty without running the decoder."""

from typing import assert_type

from pydantic import JsonValue

from pywhales._json import loads


def _check_native_json_value() -> None:
    assert_type(loads('{"items": [true, null]}'), JsonValue)
