from __future__ import annotations

import io
import json
import struct
from dataclasses import dataclass


HEADER = struct.Struct("!4sBI")
MAGIC = b"ECDC"


@dataclass(frozen=True)
class EcdcHeader:
    version: int
    model: str
    audio_length: int
    codebooks: int
    language_model: bool


def parse_header(payload: bytes) -> EcdcHeader:
    source = io.BytesIO(payload)
    fixed = source.read(HEADER.size)
    if len(fixed) != HEADER.size:
        raise ValueError("truncated ECDC header")
    magic, version, metadata_size = HEADER.unpack(fixed)
    if magic != MAGIC:
        raise ValueError("not an ECDC file")
    if version != 0:
        raise ValueError(f"unsupported ECDC version {version}")
    if not 2 <= metadata_size <= 64 * 1024:
        raise ValueError("invalid ECDC metadata size")
    metadata_bytes = source.read(metadata_size)
    if len(metadata_bytes) != metadata_size:
        raise ValueError("truncated ECDC metadata")
    metadata = json.loads(metadata_bytes)
    try:
        header = EcdcHeader(
            version=version,
            model=str(metadata["m"]),
            audio_length=int(metadata["al"]),
            codebooks=int(metadata["nc"]),
            language_model=bool(metadata["lm"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid ECDC metadata") from error
    if header.audio_length <= 0 or not 1 <= header.codebooks <= 32:
        raise ValueError("invalid ECDC audio length or codebook count")
    return header


def make_test_ecdc(
    samples: int, codebooks: int, model: str = "encodec_48khz"
) -> bytes:
    """Build a header-only ECDC value for tests; it is not playable audio."""
    metadata = json.dumps(
        {"m": model, "al": samples, "nc": codebooks, "lm": False},
        separators=(",", ":"),
    ).encode()
    return HEADER.pack(MAGIC, 0, len(metadata)) + metadata
