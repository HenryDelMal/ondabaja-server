from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Iterator

from .config import Config


LOG = logging.getLogger(__name__)


def command(config: Config) -> list[str]:
    result = [config.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin"]
    result.extend(config.input_options)

    if config.input_format:
        result.extend(["-f", config.input_format])

    result.extend(
        [
            "-i",
            config.input,
            "-map",
            "0:a:0",
            "-vn",
        ]
    )

    result.extend(config.output_options)

    result.extend(
        [
            "-ac",
            str(config.channels),
            "-ar",
            str(config.sample_rate),
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            "pipe:1",
        ]
    )
    return result


class FfmpegInput:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        argv = command(self.config)
        LOG.info("starting FFmpeg input: %r", argv)
        self.process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in iter(self.process.stderr.readline, b""):
            LOG.warning("ffmpeg: %s", line.decode(errors="replace").rstrip())

    def chunks(self, sample_frames: int) -> Iterator[bytes]:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("FFmpeg is not running")
        bytes_per_sample_frame = self.config.bytes_per_sample_frame
        wanted = sample_frames * bytes_per_sample_frame
        buffer = bytearray()
        while len(buffer) < wanted:
            block = self.process.stdout.read(wanted - len(buffer))
            if not block:
                break
            buffer.extend(block)
        while buffer:
            if len(buffer) >= wanted:
                yield bytes(buffer[:wanted])
                del buffer[:wanted]
            else:
                # Keep each published ECDC segment at the manifest's fixed duration.
                return
            while len(buffer) < wanted:
                block = self.process.stdout.read(wanted - len(buffer))
                if not block:
                    break
                buffer.extend(block)

    def wait(self) -> int:
        return self.process.wait() if self.process is not None else -1

    def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
