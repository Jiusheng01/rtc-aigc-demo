# Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
# SPDX-license-identifier: BSD-3-Clause

from __future__ import annotations

import base64
import hashlib
import hmac
import random
import struct
import time
from dataclasses import dataclass, field

VERSION = "001"
VERSION_LENGTH = 3
APP_ID_LENGTH = 24
_RANDOM_INT = int(random.random() * 0xFFFFFFFF)


class Privileges:
    PrivPublishStream = 0
    privPublishAudioStream = 1
    privPublishVideoStream = 2
    privPublishDataStream = 3
    PrivSubscribeStream = 4


privileges = Privileges


class ByteBuf:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def put_uint16(self, value: int) -> "ByteBuf":
        self.buffer.extend(struct.pack("<H", int(value)))
        return self

    def put_uint32(self, value: int) -> "ByteBuf":
        self.buffer.extend(struct.pack("<I", int(value) & 0xFFFFFFFF))
        return self

    def put_bytes(self, value: bytes) -> "ByteBuf":
        self.put_uint16(len(value))
        self.buffer.extend(value)
        return self

    def put_string(self, value: str) -> "ByteBuf":
        return self.put_bytes(value.encode())

    def put_tree_map_uint32(self, value: dict[int, int] | None) -> "ByteBuf":
        if not value:
            self.put_uint16(0)
            return self
        self.put_uint16(len(value))
        for key in sorted(value):
            self.put_uint16(key)
            self.put_uint32(value[key])
        return self

    def pack(self) -> bytes:
        return bytes(self.buffer)


class ReadByteBuf:
    def __init__(self, value: bytes) -> None:
        self.buffer = value
        self.position = 0

    def get_uint16(self) -> int:
        value = struct.unpack_from("<H", self.buffer, self.position)[0]
        self.position += 2
        return value

    def get_uint32(self) -> int:
        value = struct.unpack_from("<I", self.buffer, self.position)[0]
        self.position += 4
        return value

    def get_bytes(self) -> bytes:
        length = self.get_uint16()
        start = self.position
        self.position += length
        return self.buffer[start : start + length]

    def get_string(self) -> str:
        return self.get_bytes().decode()

    def get_tree_map_uint32(self) -> dict[int, int]:
        result: dict[int, int] = {}
        for _ in range(self.get_uint16()):
            result[self.get_uint16()] = self.get_uint32()
        return result


def _encode_hmac(key: str, message: bytes) -> bytes:
    return hmac.new(key.encode(), message, hashlib.sha256).digest()


@dataclass
class AccessToken:
    app_id: str
    app_key: str
    room_id: str
    user_id: str
    issued_at: int = field(default_factory=lambda: int(time.time()))
    nonce: int = field(default_factory=lambda: _RANDOM_INT)
    expire_at: int = 0
    privilege_map: dict[int, int] = field(default_factory=dict)
    signature: bytes | None = None

    def add_privilege(self, privilege: int, expire_timestamp: int) -> None:
        self.privilege_map[privilege] = expire_timestamp
        if privilege == Privileges.PrivPublishStream:
            self.privilege_map[Privileges.privPublishVideoStream] = expire_timestamp
            self.privilege_map[Privileges.privPublishAudioStream] = expire_timestamp
            self.privilege_map[Privileges.privPublishDataStream] = expire_timestamp

    def expire_time(self, expire_timestamp: int) -> None:
        self.expire_at = expire_timestamp

    def pack_msg(self) -> bytes:
        return (
            ByteBuf()
            .put_uint32(self.nonce)
            .put_uint32(self.issued_at)
            .put_uint32(self.expire_at)
            .put_string(self.room_id)
            .put_string(self.user_id)
            .put_tree_map_uint32(self.privilege_map)
            .pack()
        )

    def serialize(self) -> str:
        message = self.pack_msg()
        signature = _encode_hmac(self.app_key, message)
        content = ByteBuf().put_bytes(message).put_bytes(signature).pack()
        return VERSION + self.app_id + base64.b64encode(content).decode()

    def verify(self, key: str) -> bool:
        if self.expire_at > 0 and int(time.time()) > self.expire_at:
            return False
        if self.signature is None:
            return False
        return hmac.compare_digest(_encode_hmac(key, self.pack_msg()), self.signature)


def parse(raw: str) -> AccessToken | None:
    try:
        if len(raw) <= VERSION_LENGTH + APP_ID_LENGTH:
            return None
        if raw[:VERSION_LENGTH] != VERSION:
            return None
        token = AccessToken("", "", "", "")
        token.app_id = raw[VERSION_LENGTH : VERSION_LENGTH + APP_ID_LENGTH]
        content = base64.b64decode(raw[VERSION_LENGTH + APP_ID_LENGTH :])
        readbuf = ReadByteBuf(content)
        message = readbuf.get_bytes()
        token.signature = readbuf.get_bytes()
        msgbuf = ReadByteBuf(message)
        token.nonce = msgbuf.get_uint32()
        token.issued_at = msgbuf.get_uint32()
        token.expire_at = msgbuf.get_uint32()
        token.room_id = msgbuf.get_string()
        token.user_id = msgbuf.get_string()
        token.privilege_map = msgbuf.get_tree_map_uint32()
        return token
    except Exception:
        return None


Parse = parse
