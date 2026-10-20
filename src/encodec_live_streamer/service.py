from __future__ import annotations

import logging
import signal
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .config import Config
from .encoder import EncodecEncoder
from .ffmpeg import FfmpegInput
from .manifest import ManifestStore
from .tcp import TcpServer


LOG = logging.getLogger(__name__)


class Encoder(Protocol):
    def encode(self, pcm_f32le: bytes) -> bytes: ...

    def close(self) -> None: ...


class LiveService:
    def __init__(self, config: Config, encoder: Encoder | None = None) -> None:
        self.config = config.validate()
        if not self.config.segment_is_aligned:
            LOG.warning(
                "segment_duration %.6g is not aligned to %s; "
                "aligned durations reduce boundary artifacts",
                self.config.segment_duration,
                self.config.alignment_description,
            )
        self.encoder = encoder
        self.store = ManifestStore(config)
        self.stopping = threading.Event()
        self._input: FfmpegInput | None = None

    def request_stop(self, *_args: object) -> None:
        self.stopping.set()
        if self._input is not None:
            self._input.stop()

    def run(self, install_signal_handlers: bool = True) -> None:
        # Multi-radio coordination runs one publisher per station in worker
        # threads; only the coordinator/main thread may register OS handlers.
        if install_signal_handlers:
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, self.request_stop)

        first_attempt = True
        tcp_server: TcpServer | None = None
        try:
            # Refresh HTTP manifests at startup, including removal of legacy titles.
            self.store.write_manifest()
            if self.config.tcp_enabled:
                tcp_server = TcpServer(self.config, self.store)
                tcp_server.start()
            if self.encoder is None:
                self.encoder = EncodecEncoder(self.config)
            while not self.stopping.is_set():
                source = FfmpegInput(self.config)
                self._input = source
                source.start()
                epoch = str(uuid.uuid4())
                pts_samples = 0
                anchor = datetime.now(timezone.utc)
                epoch_start_unix_ms = int(anchor.timestamp() * 1000)
                first_segment = True
                published = 0
                try:
                    for pcm in source.chunks(self.config.samples_per_segment):
                        if self.stopping.is_set():
                            break
                        sample_count = len(pcm) // self.config.bytes_per_sample_frame
                        if not sample_count:
                            continue
                        assert self.encoder is not None
                        payload = self.encoder.encode(pcm)
                        timestamp = anchor + timedelta(
                            seconds=pts_samples / self.config.sample_rate
                        )
                        item = self.store.publish_segment(
                            payload,
                            sample_count=sample_count,
                            pts_samples=pts_samples,
                            program_date_time=timestamp.isoformat(timespec="milliseconds").replace(
                                "+00:00", "Z"
                            ),
                            epoch=epoch,
                            epoch_start_unix_ms=epoch_start_unix_ms,
                            discontinuity=first_segment,
                        )
                        LOG.info(
                            "published sequence=%s duration=%.3fs bytes=%s",
                            item["sequence"],
                            item["duration"],
                            item["byte_length"],
                        )
                        first_segment = False
                        first_attempt = False
                        published += 1
                        pts_samples += sample_count
                finally:
                    source.stop()
                    exit_code = source.wait()
                    self._input = None
                if self.stopping.is_set():
                    break
                if not self.config.restart_ffmpeg:
                    if exit_code != 0:
                        raise RuntimeError(f"FFmpeg exited with status {exit_code}")
                    break
                LOG.warning(
                    "FFmpeg ended with status %s after %s segments; restarting in %.1fs",
                    exit_code,
                    published,
                    self.config.restart_delay,
                )
                if self.stopping.wait(self.config.restart_delay):
                    break
                # Prevent a permanently bad command from becoming an invisible tight loop.
                if first_attempt and self.config.restart_delay == 0:
                    time.sleep(0.1)
        finally:
            if tcp_server is not None:
                tcp_server.close()
            close = getattr(self.encoder, "close", None)
            if close is not None:
                close()
