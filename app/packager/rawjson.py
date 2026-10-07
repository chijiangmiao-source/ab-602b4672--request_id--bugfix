"""Byte-exact JSON object member splitting.

The service stores extension (unknown) fields as raw byte slices of the
original request body so that a legacy terminal's unknown subtree survives
round trips byte-for-byte.  This module provides the minimal JSON lexer
needed to locate top-level object members and their exact byte spans
without re-serializing them.
"""

from __future__ import annotations

import json
from typing import List, Tuple

_WS = b" \t\r\n"
_VALUE_END = b",}] \t\r\n"


class RawJsonError(ValueError):
    """Raised when the raw bytes are not a well-formed JSON object."""


def _skip_ws(buf: bytes, i: int) -> int:
    while i < len(buf) and buf[i] in _WS:
        i += 1
    return i


def _scan_string(buf: bytes, i: int) -> int:
    """buf[i] must be the opening quote; return index past the closing quote."""
    if i >= len(buf) or buf[i] != 0x22:  # '"'
        raise RawJsonError("expected string")
    i += 1
    while i < len(buf):
        c = buf[i]
        if c == 0x5C:  # backslash escape
            i += 2
            continue
        if c == 0x22:
            return i + 1
        i += 1
    raise RawJsonError("unterminated string")


def _scan_composite(buf: bytes, i: int, open_c: int, close_c: int) -> int:
    depth = 0
    while i < len(buf):
        c = buf[i]
        if c == 0x22:
            i = _scan_string(buf, i)
            continue
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise RawJsonError("unterminated composite value")


def scan_value(buf: bytes, i: int) -> int:
    """Return the index just past the JSON value starting at or after i."""
    i = _skip_ws(buf, i)
    if i >= len(buf):
        raise RawJsonError("expected value")
    c = buf[i]
    if c == 0x22:
        return _scan_string(buf, i)
    if c == 0x7B:  # '{'
        return _scan_composite(buf, i, 0x7B, 0x7D)
    if c == 0x5B:  # '['
        return _scan_composite(buf, i, 0x5B, 0x5D)
    j = i
    while j < len(buf) and buf[j] not in _VALUE_END:
        j += 1
    if j == i:
        raise RawJsonError("expected value")
    return j


def split_object_members(buf: bytes) -> List[Tuple[str, bytes, bytes]]:
    """Split a JSON object into its top-level members.

    Returns a list of ``(key, member_raw, value_raw)`` triples where
    ``member_raw`` is the exact byte slice from the opening quote of the key
    to the end of the value, and ``value_raw`` is the exact slice of the
    value.  Raises RawJsonError on malformed input or duplicate keys.
    """
    i = _skip_ws(buf, 0)
    if i >= len(buf) or buf[i] != 0x7B:
        raise RawJsonError("expected object")
    i += 1
    members: List[Tuple[str, bytes, bytes]] = []
    seen = set()
    i = _skip_ws(buf, i)
    if i < len(buf) and buf[i] == 0x7D:
        i = _skip_ws(buf, i + 1)
        if i != len(buf):
            raise RawJsonError("trailing data after object")
        return members
    while True:
        i = _skip_ws(buf, i)
        mstart = i
        kend = _scan_string(buf, i)
        key = json.loads(buf[mstart:kend].decode("utf-8"))
        if key in seen:
            raise RawJsonError(f"duplicate key: {key!r}")
        seen.add(key)
        i = _skip_ws(buf, kend)
        if i >= len(buf) or buf[i] != 0x3A:  # ':'
            raise RawJsonError("expected ':'")
        i = _skip_ws(buf, i + 1)
        vstart = i
        vend = scan_value(buf, i)
        members.append((key, buf[mstart:vend], buf[vstart:vend]))
        i = _skip_ws(buf, vend)
        if i >= len(buf):
            raise RawJsonError("unterminated object")
        if buf[i] == 0x2C:  # ','
            i += 1
            continue
        if buf[i] == 0x7D:  # '}'
            i = _skip_ws(buf, i + 1)
            if i != len(buf):
                raise RawJsonError("trailing data after object")
            return members
        raise RawJsonError("expected ',' or '}'")


def find_member(buf: bytes, name: str) -> bytes | None:
    """Return the raw value slice of a top-level member, or None."""
    for key, _member, value in split_object_members(buf):
        if key == name:
            return value
    return None
