"""File hashes used by Modrinth and CurseForge."""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path


CHUNK_SIZE = 1024 * 1024
CURSEFORGE_WHITESPACE = b"\x09\x0a\x0d\x20"
MASK = 0xFFFFFFFF
M = 0x5BD1E995


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def curseforge_fingerprint(path: Path) -> int:
    """CurseForge's MurmurHash2 variant: seed 1, ignoring four whitespace bytes.

    MurmurHash2 seeds its state with the normalized byte length, so a first
    pass counts bytes and a second pass hashes them without loading a JAR into
    memory.
    """
    normalized_length = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            normalized_length += len(chunk.translate(None, CURSEFORGE_WHITESPACE))

    result = (1 ^ normalized_length) & MASK
    remaining = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            data = remaining + chunk.translate(None, CURSEFORGE_WHITESPACE)
            full_length = len(data) & ~3
            for offset in range(0, full_length, 4):
                (value,) = struct.unpack_from("<I", data, offset)
                value = (value * M) & MASK
                value ^= value >> 24
                value = (value * M) & MASK
                result = ((result * M) & MASK) ^ value
            remaining = data[full_length:]

    if len(remaining) == 3:
        result ^= remaining[2] << 16
    if len(remaining) >= 2:
        result ^= remaining[1] << 8
    if remaining:
        result ^= remaining[0]
        result = (result * M) & MASK

    result ^= result >> 13
    result = (result * M) & MASK
    result ^= result >> 15
    return result & MASK
