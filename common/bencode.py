from __future__ import annotations

from typing import Any


class BencodeError(ValueError):
    """Raised when bencoded data is invalid or an unsupported value is encoded."""


def bencode(value: Any) -> bytes:
    """
    Encode a Python value using canonical Bencode.

    Supported types:
        int, bytes, str, list, tuple, dict
    """
    if isinstance(value, bool):
        raise BencodeError("bool is not supported; use an int instead")

    if isinstance(value, int):
        return f"i{value}e".encode("ascii")

    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value

    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return str(len(encoded)).encode("ascii") + b":" + encoded

    if isinstance(value, (list, tuple)):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"

    if isinstance(value, dict):
        items: list[tuple[bytes, Any]] = []

        for key, item_value in value.items():
            if isinstance(key, str):
                key_bytes = key.encode("utf-8")
            elif isinstance(key, bytes):
                key_bytes = key
            else:
                raise BencodeError("dictionary keys must be str or bytes")

            items.append((key_bytes, item_value))

        items.sort(key=lambda pair: pair[0])

        body = bytearray()
        for key_bytes, item_value in items:
            body.extend(bencode(key_bytes))
            body.extend(bencode(item_value))

        return b"d" + bytes(body) + b"e"

    raise BencodeError(f"unsupported type: {type(value).__name__}")


class _BencodeParser:
    def __init__(self, data: bytes):
        if not isinstance(data, bytes):
            raise TypeError("bdecode expects bytes")
        self.data = data
        self.index = 0

    def parse(self) -> Any:
        value = self._parse_value()
        if self.index != len(self.data):
            raise BencodeError(f"extra data found after position {self.index}")
        return value

    def _parse_value(self) -> Any:
        if self.index >= len(self.data):
            raise BencodeError("unexpected end of data")

        marker = self.data[self.index:self.index + 1]

        if marker == b"i":
            return self._parse_integer()
        if marker == b"l":
            return self._parse_list()
        if marker == b"d":
            return self._parse_dictionary()
        if b"0" <= marker <= b"9":
            return self._parse_bytes()

        raise BencodeError(
            f"invalid marker {marker!r} at position {self.index}"
        )

    def _parse_integer(self) -> int:
        self.index += 1
        end_index = self.data.find(b"e", self.index)

        if end_index == -1:
            raise BencodeError("unterminated integer")

        raw_number = self.data[self.index:end_index]

        if not raw_number:
            raise BencodeError("empty integer")
        if raw_number == b"-0":
            raise BencodeError("negative zero is invalid")
        if raw_number.startswith(b"0") and raw_number != b"0":
            raise BencodeError("leading zero in integer")
        if raw_number.startswith(b"-0"):
            raise BencodeError("leading zero in negative integer")

        try:
            number = int(raw_number)
        except ValueError as exc:
            raise BencodeError(f"invalid integer: {raw_number!r}") from exc

        self.index = end_index + 1
        return number

    def _parse_bytes(self) -> bytes:
        colon_index = self.data.find(b":", self.index)

        if colon_index == -1:
            raise BencodeError("missing ':' in byte string")

        raw_length = self.data[self.index:colon_index]

        if not raw_length:
            raise BencodeError("empty byte-string length")
        if raw_length.startswith(b"0") and raw_length != b"0":
            raise BencodeError("leading zero in byte-string length")

        try:
            length = int(raw_length)
        except ValueError as exc:
            raise BencodeError(
                f"invalid byte-string length: {raw_length!r}"
            ) from exc

        start = colon_index + 1
        end = start + length

        if end > len(self.data):
            raise BencodeError("byte string exceeds available data")

        self.index = end
        return self.data[start:end]

    def _parse_list(self) -> list[Any]:
        self.index += 1
        result: list[Any] = []

        while True:
            if self.index >= len(self.data):
                raise BencodeError("unterminated list")
            if self.data[self.index:self.index + 1] == b"e":
                self.index += 1
                return result
            result.append(self._parse_value())

    def _parse_dictionary(self) -> dict[bytes, Any]:
        self.index += 1
        result: dict[bytes, Any] = {}
        previous_key: bytes | None = None

        while True:
            if self.index >= len(self.data):
                raise BencodeError("unterminated dictionary")
            if self.data[self.index:self.index + 1] == b"e":
                self.index += 1
                return result

            key = self._parse_value()

            if not isinstance(key, bytes):
                raise BencodeError("dictionary key must be a byte string")
            if previous_key is not None and key < previous_key:
                raise BencodeError("dictionary keys are not sorted")
            if key in result:
                raise BencodeError(f"duplicate dictionary key: {key!r}")

            previous_key = key
            result[key] = self._parse_value()


def bdecode(data: bytes) -> Any:
    """Decode Bencoded bytes while preserving strings as bytes."""
    return _BencodeParser(data).parse()