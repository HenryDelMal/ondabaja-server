from __future__ import annotations

import logging
import shutil
import struct
import subprocess
import threading

from .config import Config
from .ecdc import parse_header


LOG = logging.getLogger(__name__)
SIZE = struct.Struct("<I")
MAX_SEGMENT_BYTES = 256 * 1024 * 1024


def native_encoder_path(config: Config) -> str:
    resolved = shutil.which(config.native_encoder)
    return resolved or config.native_encoder


def native_command(config: Config, *, check_model: bool = False) -> list[str]:
    result = [
        native_encoder_path(config),
        "--codec",
        config.codec,
        "--model",
        str(config.model_path),
        "--samplerate",
        str(config.samplerate),
        "--codebooks",
        str(config.codebooks),
        "--threads",
        str(config.threads),
        "--beam-size",
        str(1 if config.codec == "ECNG" else config.beam_size),
    ]
    if check_model:
        result.append("--check-model")
    return result


class EncodecEncoder:
    """Persistent wrapper around the portable C++ EnCodec encoder worker."""

    def __init__(self, config: Config) -> None:
        argv = native_command(config)
        LOG.info(
            "starting C++ EnCodec encoder model=%s bandwidth=%g kbps threads=%s beam_size=%s",
            config.model,
            config.bandwidth_kbps,
            config.threads,
            config.beam_size,
        )
        self.config = config
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._stderr: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in iter(self.process.stderr.readline, b""):
            message = line.decode(errors="replace").rstrip()
            self._stderr.append(message)
            self._stderr[:] = self._stderr[-20:]
            LOG.warning("native encoder: %s", message)

    @staticmethod
    def _read_exact(stream: object, count: int) -> bytes:
        result = bytearray()
        while len(result) < count:
            block = stream.read(count - len(result))  # type: ignore[attr-defined]
            if not block:
                break
            result.extend(block)
        return bytes(result)

    def encode(self, pcm_f32le: bytes) -> bytes:
        if not pcm_f32le or len(pcm_f32le) % self.config.bytes_per_sample_frame:
            raise ValueError("PCM does not contain complete float32 sample frames")
        if len(pcm_f32le) > MAX_SEGMENT_BYTES:
            raise ValueError("PCM segment is too large for the native worker protocol")
        if self.process.poll() is not None:
            detail = self._stderr[-1] if self._stderr else f"status {self.process.returncode}"
            raise RuntimeError(f"native encoder stopped: {detail}")
        assert self.process.stdin is not None and self.process.stdout is not None
        try:
            self.process.stdin.write(SIZE.pack(len(pcm_f32le)))
            self.process.stdin.write(pcm_f32le)
            self.process.stdin.flush()
        except BrokenPipeError as error:
            detail = self._stderr[-1] if self._stderr else "broken pipe"
            raise RuntimeError(f"native encoder failed: {detail}") from error
        fixed = self._read_exact(self.process.stdout, SIZE.size)
        if len(fixed) != SIZE.size:
            detail = self._stderr[-1] if self._stderr else "no response"
            raise RuntimeError(f"native encoder failed: {detail}")
        payload_size = SIZE.unpack(fixed)[0]
        if not 1 <= payload_size <= MAX_SEGMENT_BYTES:
            raise RuntimeError("native encoder returned an invalid segment size")
        payload = self._read_exact(self.process.stdout, payload_size)
        if len(payload) != payload_size:
            raise RuntimeError("native encoder returned a truncated segment")
        header = parse_header(payload)
        if (
            header.model != self.config.model
            or header.codebooks != self.config.codebooks
            or header.language_model
        ):
            raise RuntimeError("native encoder produced an incompatible ECDC segment")
        return payload

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            assert self.process.stdin is not None
            self.process.stdin.write(SIZE.pack(0))
            self.process.stdin.flush()
            self.process.stdin.close()
            self.process.wait(timeout=5)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
