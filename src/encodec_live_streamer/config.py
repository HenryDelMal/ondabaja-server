from __future__ import annotations

import dataclasses
import logging
import pathlib
from typing import Any

try:
    import tomllib
except ImportError:  # Python 3.9 and 3.10
    import tomli as tomllib


MODEL_PROFILES = {
    24: {
        "model": "encodec_24khz",
        "sample_rate": 24_000,
        "channels": 1,
        "frame_rate": 75,
        "alignment_samples": 320,
        "bandwidths": {1.5: 2, 3.0: 4, 6.0: 8, 12.0: 16, 24.0: 32},
    },
    48: {
        "model": "encodec_48khz",
        "sample_rate": 48_000,
        "channels": 2,
        "frame_rate": 150,
        "alignment_samples": 47_520,
        "bandwidths": {3.0: 2, 6.0: 4, 12.0: 8, 24.0: 16},
    },
}


@dataclasses.dataclass(frozen=True)
class Config:
    input: str
    output_dir: pathlib.Path
    codec: str = "EnCodec"
    input_format: str | None = None
    input_options: tuple[str, ...] = ()
    output_options: tuple[str, ...] = ()
    ffmpeg: str = "ffmpeg"
    native_encoder: str = "encodec-live-native"
    model_dir: pathlib.Path = pathlib.Path("/opt/encodec-live/models")
    samplerate: int = 24
    bandwidth_kbps: float = 3.0
    # 149 codec frames at 75 Hz: the closest clean 24 kHz segment to 2 seconds.
    segment_duration: float = 149 / 75
    window_segments: int = 16
    stale_grace_segments: int = 6
    threads: int = 1
    beam_size: int = 8
    restart_ffmpeg: bool = True
    restart_delay: float = 2.0
    manifest_name: str = "stream.json"
    fsync: bool = True
    tcp_enabled: bool = False
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 9001
    tcp_max_clients: int = 32
    tcp_idle_timeout: float = 60.0
    tcp_manifest_wait: float = 15.0
    tcp_heartbeat_enabled: bool = False
    tcp_heartbeat_interval: float = 3.5
    tcp_max_retries: int = 2
    tcp_diagnostics: bool = False
    tcp_segment_cache_bytes: int = 8 * 1024 * 1024

    @property
    def profile(self) -> dict[str, Any]:
        if self.codec == "ECNG":
            return {
                "model": "ecng_24khz", "sample_rate": 24_000, "channels": 1,
                "frame_rate": 75, "alignment_samples": 320,
                "bandwidths": {1.5: 2, 3.0: 4},
            }
        return MODEL_PROFILES[self.samplerate]

    @property
    def model(self) -> str:
        return str(self.profile["model"])

    @property
    def sample_rate(self) -> int:
        return int(self.profile["sample_rate"])

    @property
    def channels(self) -> int:
        return int(self.profile["channels"])

    @property
    def frame_rate(self) -> int:
        return int(self.profile["frame_rate"])

    @property
    def model_path(self) -> pathlib.Path:
        if self.codec == "ECNG":
            return self.model_dir / "ecng-24khz-encoder-f32.bin"
        return self.model_dir / f"{self.model}-combined-f32.bin"

    @property
    def bytes_per_sample_frame(self) -> int:
        return self.channels * 4  # interleaved float32

    @property
    def samples_per_segment(self) -> int:
        return round(self.segment_duration * self.sample_rate)

    @property
    def codebooks(self) -> int:
        bandwidths: dict[float, int] = self.profile["bandwidths"]
        return bandwidths[self.bandwidth_kbps]

    @property
    def segment_is_aligned(self) -> bool:
        return self.samples_per_segment % int(self.profile["alignment_samples"]) == 0

    @property
    def alignment_description(self) -> str:
        if self.samplerate == 48:
            return "the 48 kHz model's 0.99-second stride"
        return "the 24 kHz model's 320-sample (1/75-second) codec frame"

    def validate(self) -> Config:
        if self.codec not in {"EnCodec", "ECNG"}:
            raise ValueError('codec must be "EnCodec" or "ECNG"')
        if self.codec == "ECNG" and self.samplerate != 24:
            raise ValueError("ECNG supports only samplerate=24")
        if not self.input:
            raise ValueError("input must not be empty")
        if isinstance(self.samplerate, bool) or not isinstance(self.samplerate, int):
            raise ValueError("samplerate must be the integer 24 or 48")
        if self.samplerate not in MODEL_PROFILES:
            raise ValueError("samplerate must be 24 or 48")
        bandwidths: dict[float, int] = self.profile["bandwidths"]
        if self.bandwidth_kbps not in bandwidths:
            supported = ", ".join(f"{value:g}" for value in bandwidths)
            raise ValueError(
                f"bandwidth_kbps for samplerate={self.samplerate} must be one of {supported}"
            )
        if not 0.25 <= self.segment_duration <= 30:
            raise ValueError("segment_duration must be between 0.25 and 30 seconds")
        if self.samples_per_segment <= 0:
            raise ValueError("segment_duration is too small")
        if self.window_segments < 2:
            raise ValueError("window_segments must be at least 2")
        if self.stale_grace_segments < 0:
            raise ValueError("stale_grace_segments cannot be negative")
        if (
            isinstance(self.threads, bool)
            or not isinstance(self.threads, int)
            or self.threads < 1
        ):
            raise ValueError("threads must be an integer of at least 1")
        if (
            isinstance(self.beam_size, bool)
            or not isinstance(self.beam_size, int)
            or not 1 <= self.beam_size <= 64
        ):
            raise ValueError("beam_size must be an integer between 1 and 64")
        if self.restart_delay < 0:
            raise ValueError("restart_delay cannot be negative")
        if pathlib.Path(self.manifest_name).name != self.manifest_name:
            raise ValueError("manifest_name must be a file name, not a path")
        if not isinstance(self.tcp_enabled, bool):
            raise ValueError("tcp_enabled must be a boolean")
        if not isinstance(self.tcp_heartbeat_enabled, bool):
            raise ValueError("tcp_heartbeat_enabled must be a boolean")
        if not isinstance(self.tcp_diagnostics, bool):
            raise ValueError("tcp_diagnostics must be a boolean")
        if (isinstance(self.tcp_segment_cache_bytes, bool)
                or not isinstance(self.tcp_segment_cache_bytes, int)
                or not 0 <= self.tcp_segment_cache_bytes <= 1024 * 1024 * 1024):
            raise ValueError("tcp_segment_cache_bytes must be between 0 and 1073741824")
        if not isinstance(self.tcp_host, str) or not self.tcp_host.strip():
            raise ValueError("tcp_host must be a nonempty bind address")
        for name, minimum, maximum in (
            ("tcp_port", 1, 65535),
            ("tcp_max_clients", 1, 1024),
            ("tcp_max_retries", 0, 10),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
        for name, minimum, maximum in (
            ("tcp_idle_timeout", 1, 3600),
            ("tcp_manifest_wait", 0.1, 300),
            ("tcp_heartbeat_interval", 0.1, 60),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} must be between {minimum} and {maximum} seconds")
        if self.tcp_manifest_wait >= self.tcp_idle_timeout:
            raise ValueError("tcp_manifest_wait must be less than tcp_idle_timeout")
        return self

    @classmethod
    def from_toml(cls, path: str | pathlib.Path, *, default_codec: str | None = None,
                  defaults: dict[str, Any] | None = None) -> Config:
        source = pathlib.Path(path)
        with source.open("rb") as handle:
            raw = tomllib.load(handle)

        table: dict[str, Any] = dict(raw.get("stream", raw))
        if "title" in table:
            table.pop("title")
            logging.getLogger(__name__).warning(
                "title is no longer published; remove it from the TOML configuration"
            )
        allowed = {field.name for field in dataclasses.fields(cls)}
        unknown = set(table) - allowed
        if unknown:
            raise ValueError(
                f"unknown configuration keys: {', '.join(sorted(unknown))}"
            )

        values = dict(defaults or {})
        values.update(table)
        if default_codec is not None:
            values.setdefault("codec", default_codec)
        missing = {"input", "output_dir"} - set(values)
        if missing:
            raise ValueError(
                f"missing configuration keys: {', '.join(sorted(missing))}"
            )

        values["output_dir"] = pathlib.Path(values["output_dir"])
        if "model_dir" in values:
            values["model_dir"] = pathlib.Path(values["model_dir"])

        for name in ("input_options", "output_options"):
            if name in values:
                if not isinstance(values[name], list):
                    raise ValueError(f"{name} must be a TOML array of strings")
                values[name] = tuple(str(item) for item in values[name])

        return cls(**values).validate()
