from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import pathlib
import signal
import threading
try:
    import tomllib
except ImportError:
    import tomli as tomllib
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from .config import Config
from .mux import OndaBajaTcpServer, Radio
from .manifest import atomic_write
from .service import LiveService

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class StationConfig:
    id: str
    title: str
    manifest_url: str
    config_path: pathlib.Path


@dataclass(frozen=True)
class OndaBajaConfig:
    host: str
    port: int
    max_clients: int
    idle_timeout: float
    fetch_wait: float
    max_streams: int
    revision_file: pathlib.Path
    stations: tuple[StationConfig, ...]
    revision: int
    output_root: pathlib.Path


def _revision(path: pathlib.Path, stations: tuple[StationConfig, ...], defaults: dict | None = None) -> int:
    fingerprint = hashlib.sha256()
    fingerprint.update(json.dumps(defaults or {}, sort_keys=True, default=str).encode())
    for station in stations:
        fingerprint.update(station.id.encode())
        fingerprint.update(b"\0")
        fingerprint.update(station.title.encode())
        fingerprint.update(b"\0")
        fingerprint.update(station.manifest_url.encode())
        fingerprint.update(b"\0")
        fingerprint.update(station.config_path.read_bytes())
        fingerprint.update(b"\0")
    digest = fingerprint.hexdigest()
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}
    try:
        revision = int(current.get("revision", 0))
    except (TypeError, ValueError):
        revision = 0
    if not 0 <= revision < (1 << 64) - 1:
        revision = 0
    if current.get("fingerprint") != digest:
        revision += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps({"revision": revision, "fingerprint": digest}) + "\n",
                             encoding="utf-8")
        temporary.replace(path)
    return revision


def load_ondabaja_config(path: str | pathlib.Path) -> tuple[OndaBajaConfig, tuple[Config, ...]]:
    source = pathlib.Path(path).resolve()
    with source.open("rb") as handle:
        raw = tomllib.load(handle)
    server = raw.get("server", {})
    defaults = dict(raw.get("stream_defaults", {}))
    allowed_defaults = {field.name for field in dataclasses.fields(Config)}
    if set(defaults) - allowed_defaults:
        raise ValueError(f"unknown stream_defaults keys: {sorted(set(defaults) - allowed_defaults)}")
    # Global paths are relative to server.toml. Station overrides remain
    # relative to their own TOML file.
    if "model_dir" in defaults:
        defaults["model_dir"] = str((source.parent / defaults["model_dir"]).resolve())
    for executable in ("native_encoder", "ffmpeg"):
        if executable in defaults and "/" in defaults[executable]:
            defaults[executable] = str((source.parent / defaults[executable]).resolve())
    if "output_dir" in defaults:
        raise ValueError("use server.output_root instead of stream_defaults.output_dir")
    output_root = (source.parent / server.get("output_root", "public")).resolve()
    public_base_url = str(server.get("public_base_url", "")).strip().rstrip("/")
    parsed_base = urlsplit(public_base_url)
    if public_base_url and (parsed_base.scheme not in {"http", "https"}
                            or not parsed_base.netloc or parsed_base.query or parsed_base.fragment):
        raise ValueError("server.public_base_url must be an HTTP(S) directory URL without query or fragment")
    if "stations" in raw:
        raise ValueError("put station configuration files in server.stations_dir")
    directory = (source.parent / server.get("stations_dir", "config/stations")).resolve()
    if not directory.is_dir():
        raise ValueError(f"station directory does not exist: {directory}")
    station_rows = []
    for station_path in sorted(directory.glob("*.toml")):
        with station_path.open("rb") as handle:
            station_document = tomllib.load(handle)
        metadata = station_document
        if not isinstance(metadata, dict):
            raise ValueError(f"{station_path}: station settings must be a flat TOML table")
        station_rows.append(dict(metadata, id=metadata.get("id", station_path.stem),
                                 config_path=str(station_path)))
    if not station_rows:
        raise ValueError("OndaBaja config requires at least one station TOML file")
    stations: list[StationConfig] = []
    streams: list[Config] = []
    seen: set[str] = set()
    output_paths: set[pathlib.Path] = set()
    for row in station_rows:
        station_id = str(row.get("id", "")).strip()
        title = str(row.get("title", row.get("name", ""))).strip()
        manifest_url = str(row.get("manifest_url", "")).strip()
        if not manifest_url and public_base_url:
            manifest_url = f"{public_base_url}/{quote(station_id, safe='')}/stream.json"
        config_name = str(row.get("config_path", "")).strip()
        if not station_id or len(station_id.encode("utf-8")) > 128:
            raise ValueError("station id must be 1..128 UTF-8 bytes")
        if station_id in seen:
            raise ValueError(f"duplicate station id: {station_id}")
        if not title or not config_name:
            raise ValueError(f"station {station_id!r} needs title and a TOML file")
        parsed_url = urlsplit(manifest_url)
        if manifest_url and (parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc):
            raise ValueError(f"station {station_id!r}: manifest_url must be HTTP or HTTPS")
        seen.add(station_id)
        config_path = pathlib.Path(config_name).resolve()
        station = StationConfig(station_id, title, manifest_url, config_path)
        # IDs are used as directory names for automatically assigned outputs.
        if station_id in {".", ".."} or "/" in station_id or "\\" in station_id:
            raise ValueError(f"station {station_id!r}: ID must not contain path separators")
        station_defaults = dict(defaults, output_dir=str(output_root / station_id))
        stream = Config.from_toml(config_path, default_codec="ECNG", defaults=station_defaults,
                                  station_config=True)
        if stream.codec != "ECNG":
            raise ValueError(f"station {station_id!r}: OndaBaja stations require codec=ECNG")
        if stream.tcp_enabled:
            LOG.warning("station %s: disabling legacy per-stream ELTCP; OndaBaja owns the mux port", station_id)
            stream = dataclasses.replace(stream, tcp_enabled=False)
        stream = dataclasses.replace(stream,
            output_dir=(config_path.parent / stream.output_dir).resolve(),
            model_dir=(config_path.parent / stream.model_dir).resolve(),
            native_encoder=(str((config_path.parent / stream.native_encoder).resolve())
                            if "/" in stream.native_encoder else stream.native_encoder),
            ffmpeg=(str((config_path.parent / stream.ffmpeg).resolve())
                    if "/" in stream.ffmpeg else stream.ffmpeg))
        output_path = stream.output_dir
        if output_path in output_paths:
            raise ValueError(f"station {station_id!r}: every station needs a unique output_dir")
        output_paths.add(output_path)
        stations.append(station)
        streams.append(stream)

    max_streams = int(server.get("max_streams", 4))
    if max_streams < 1:
        raise ValueError("server.max_streams must be positive")
    if len(stations) > max_streams:
        raise ValueError(f"configured stations ({len(stations)}) exceed server.max_streams ({max_streams})")
    host = str(server.get("host", "0.0.0.0"))
    port = int(server.get("port", 9010))
    max_clients = int(server.get("max_clients", 4096))
    idle_timeout = float(server.get("idle_timeout", 60.0))
    fetch_wait = float(server.get("fetch_wait", 15.0))
    if not 1 <= port <= 65535 or not 1 <= max_clients <= 65536:
        raise ValueError("server.port or server.max_clients is out of range")
    if idle_timeout <= fetch_wait or fetch_wait <= 0:
        raise ValueError("server.idle_timeout must exceed positive server.fetch_wait")
    revision_file = (source.parent / str(server.get("revision_file", output_root / ".ondabaja-revision.json"))).resolve()
    station_tuple = tuple(stations)
    config = OndaBajaConfig(host, port, max_clients, idle_timeout, fetch_wait,
                            max_streams, revision_file, station_tuple,
                            _revision(revision_file, station_tuple, defaults), output_root)
    return config, tuple(streams)


class OndaBajaService:
    """Runs one isolated publisher per station and a shared selection listener."""

    def __init__(self, config: OndaBajaConfig, stream_configs: tuple[Config, ...],
                 config_path: str | pathlib.Path = "server.toml"):
        self.config = config
        self.config_path = pathlib.Path(config_path).resolve()
        self.services = {station.id: LiveService(stream, segment_cache_enabled=True)
                         for station, stream in zip(config.stations, stream_configs)}
        radios = self._radios(config.stations, self.services)
        self.tcp = OndaBajaTcpServer(config.host, config.port, radios, config.revision,
                                     config.idle_timeout, config.fetch_wait, config.max_clients)
        self.stopping = threading.Event()
        self.reload_requested = threading.Event()
        self.errors: list[BaseException] = []
        self.threads: dict[str, threading.Thread] = {}
        self._retiring_services: set[str] = set()

    @staticmethod
    def _radios(stations: tuple[StationConfig, ...],
                services: dict[str, LiveService]) -> tuple[Radio, ...]:
        return tuple(Radio(station.id, station.title, station.manifest_url,
                           services[station.id].store) for station in stations)

    def stop(self, *_args) -> None:
        self.stopping.set()
        for service in self.services.values():
            service.request_stop()

    def _start_station(self, station: StationConfig, service: LiveService) -> None:
        def run_station() -> None:
            try:
                service.run(install_signal_handlers=False)
            except BaseException as error:
                # A replaced station may stop as part of a normal reload.
                if (self.services.get(station.id) is service
                        and station.id not in self._retiring_services):
                    self.errors.append(error)
                    LOG.exception("station publisher %s failed", station.id)
                    self.stop()

        thread = threading.Thread(target=run_station, name=f"radio-{station.id}", daemon=True)
        self.threads[station.id] = thread
        thread.start()

    def _reload(self) -> None:
        LOG.info("reloading station configuration from %s", self.config_path)
        new_config, stream_configs = load_ondabaja_config(self.config_path)
        if (new_config.host, new_config.port) != (self.config.host, self.config.port):
            raise ValueError("server.host or server.port changed; restart the service to rebind TCP")

        old_streams = {station_id: service.config
                       for station_id, service in self.services.items()}
        new_services: dict[str, LiveService] = {}
        new_stream_by_id = {station.id: stream
                            for station, stream in zip(new_config.stations, stream_configs)}
        for station in new_config.stations:
            previous = self.services.get(station.id)
            if previous is not None and old_streams[station.id] == new_stream_by_id[station.id]:
                new_services[station.id] = previous
            else:
                new_services[station.id] = LiveService(
                    new_stream_by_id[station.id], segment_cache_enabled=True)

        replaced = {station_id: service for station_id, service in self.services.items()
                    if new_services.get(station_id) is not service}
        self._retiring_services.update(replaced)
        for service in replaced.values():
            service.request_stop()
        for station_id in replaced:
            thread = self.threads.pop(station_id, None)
            if thread is not None:
                thread.join(timeout=15)
                if thread.is_alive():
                    raise RuntimeError(f"station publisher {station_id} did not stop for reload")
        self._retiring_services.difference_update(replaced)

        self.config = new_config
        self.services = new_services
        self.tcp.idle_timeout = new_config.idle_timeout
        self.tcp.wait_seconds = new_config.fetch_wait
        self.tcp.max_clients = new_config.max_clients
        self.tcp.update_catalog(self._radios(new_config.stations, new_services),
                                new_config.revision)
        for station in new_config.stations:
            if station.id not in self.threads:
                self._start_station(station, new_services[station.id])
        self.publish_catalog()
        self.tcp.notify_catalog_changed()
        LOG.info("reloaded %d stations revision=%d", len(new_config.stations),
                 new_config.revision)

    def run(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, self.stop)
        signal.signal(signal.SIGHUP, lambda *_args: self.reload_requested.set())

        try:
            self.publish_catalog()
            for station in self.config.stations:
                self._start_station(station, self.services[station.id])
            self.tcp.start()
            LOG.info("serving %d stations at %s revision=%d", len(self.services),
                     self.tcp.address, self.config.revision)
            while not self.stopping.wait(0.25):
                if self.reload_requested.is_set():
                    self.reload_requested.clear()
                    try:
                        self._reload()
                    except (OSError, ValueError, RuntimeError) as error:
                        LOG.exception("station reload failed; keeping the active configuration: %s",
                                      error)
                if self.errors:
                    break
            self.stop()
        finally:
            self.tcp.close()
            for thread in self.threads.values():
                thread.join(timeout=10)
        if self.errors:
            raise RuntimeError(f"station publisher stopped after error: {self.errors[0]}")

    def publish_catalog(self) -> None:
        """Publish the same directory over HTTP as the shared TCP listener."""
        document = {
            "revision": self.config.revision,
            "updated_at_unix_ms": self.tcp.catalog_updated_at_unix_ms,
            "stations": [dict(id=station.id, title=station.title,
                              manifest_url=station.manifest_url)
                         for station in self.config.stations],
        }
        atomic_write(self.config.output_root / "catalog.pb", self.tcp.catalog)
        atomic_write(self.config.output_root / "catalog.json",
                     (json.dumps(document, ensure_ascii=False) + "\n").encode("utf-8"))
