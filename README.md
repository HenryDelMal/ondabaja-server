# OndaBaja Server

Experimental multi-radio live service for OndaBaja. It keeps one HTTP live
manifest and rolling ECDC segment window per configured radio and adds a
separate persistent TCP endpoint for catalog refresh, station selection, and
absolute-sequence segment fetches. Each radio is encoded once and shared by its
listeners. The existing single-stream ELTCP v1 listener remains available in
the underlying publisher and is not changed by the OndaBaja protocol.

The project is experimental. It supports Meta EnCodec and the jointly trained
ECNG profile (24 kHz mono, two or four codebooks at 1.5 or 3 kbps). ECNG uses
the checkpoint-specific FP32 encoder in `models/ecng-24khz-encoder-f32.bin`;
the OndaBaja Android APK carries its matching INT8 decoder. The export
provenance is recorded in `models/manifest.json`. EnCodec is
Meta's research codec; this service
uses the [official Meta EnCodec repository](https://github.com/facebookresearch/encodec)
for model/checkpoint provenance and the MIT-licensed native implementation in
this repository's upstream publisher.

## What is included

- Existing FFmpeg ingest, native EnCodec worker, atomic manifest/segment
  publication, and bounded per-stream HTTP/TCP caches.
- A new OndaBaja TCP listener with a versionable protobuf catalog, monotonic
  persisted revision, stable station IDs, per-radio manifests, and the
  existing `f`/`S` absolute-sequence framing.
- Radio switches on the existing socket. A switch arriving during a future
  segment publication wait cancels that fetch and returns the selected radio's
  manifest without waiting for its timeout.
- Independent sequence, manifest, discontinuity, and cache state per radio.

## Configuration

Install Linux build dependencies (`build-essential`, `cmake`, `ffmpeg`, `git`,
`python3`, and `python3-venv`), then install the service and build its native
encoder from the EnCodec Live Streamer base:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
./scripts/build-native.sh
```

Copy your current ECNG encoder into `models/ecng-24khz-encoder-f32.bin`.
Model weights are intentionally excluded from GitHub. Use
`codec = "ECNG"` in a stream TOML for ECNG; its only valid settings are
`samplerate = 24` and `bandwidth_kbps = 1.5` or `3`. Use `codec = "EnCodec"`
for the original Meta models and bitrate profiles.

The root `server.toml` defines global settings and shared encoding defaults:

```toml
[server]
host = "0.0.0.0"
port = 9010
stations_dir = "config/stations"
output_root = "public"
public_base_url = ""  # Optional HTTP directory URL, configured privately.
max_clients = 32
max_streams = 2

[stream_defaults]
codec = "ECNG"
model_dir = "models"
native_encoder = "bin/encodec-live-native"
bandwidth_kbps = 3
threads = 1
```

The server reads `*.toml` files in the stations folder in filename order.
A station can inherit all global defaults:

```toml
[station]
id = "radio-a"
title = "Radio A"

[stream]
input = "https://radio.example.com/live.mp3"
# bandwidth_kbps = 1.5  # Optional per-station override.
```

Global paths resolve against `server.toml`; per-station path overrides resolve
against the station file. Outputs default to `public/<station id>/`.
The catalog is built as protobuf from each file's `id` and `title`, with an
optional `manifest_url` for HTTP fallback. All clients share the same TCP port,
with independent connections and station selections. Every configured radio
has one shared ECNG encoder and FFmpeg input.

The active encoder is in `models/ecng-24khz-encoder-f32.bin`. Its current export
is **radio-finetune-v1-step-000100000**; see `models/manifest.json` for encoder
and decoder checksums. Future exports can replace the active model without
changing its filename or the station TOMLs. Clients need the matching decoder.
The previous bundled encoder was preserved under `models/archive/`.

Run from the repository root (`--config` defaults to `server.toml`):

```sh
ondabaja-server check
ondabaja-server serve
```

Replace the example input URLs before serving. Add, remove, or edit station
files and restart to rebuild the catalog and update its persisted revision.
Live configuration reload is not implemented. Set `max_streams` to a count
of simultaneous encoders the host can sustain.

`server.output_root` is the shared publication directory. By default each
station writes into `<output_root>/<id>/`; omit station `output_dir` overrides
to use this layout. The server publishes `catalog.pb` and `catalog.json` at
the root when it starts. Both contain the same revision, UTC timestamp and
station metadata as the TCP catalog. Per-station `stream.json`, `stream.pb`,
and rolling segments live in each station directory. Revision state defaults
to `<output_root>/.ondabaja-revision.json` and must persist across restarts.

Set `server.public_base_url` to the HTTP directory URL mapped to `output_root`.
The server derives `<public_base_url>/<id>/stream.json` for each station's
catalog entry. An explicit station `manifest_url` overrides that URL.
HTTP publication continues independently for older clients and as a fallback.
The separate mux TCP listener defaults to port 9010. It is plain, unencrypted
TCP; expose it only on a trusted network or through a secure tunnel.

See [the OndaBaja protocol notes](proto/README.md) and the inherited
[ELTCP v1 protocol](docs/TCP_PROTOCOL.md). The TCP protocol is not compatible
with the current OndaBaja app until the app learns the catalog/selection
commands and response demultiplexing for `m` arriving during a pending `f`.

## Resource and deployment notes

The first implementation starts all configured radio publishers together.
This keeps every station at the live edge and avoids a cold-start wait after
selection, at the cost of one encoder and FFmpeg process per station. It does
not yet provide idle shutdown or demand-driven encoding. Keep the radio count
small on CPU-limited hosts. Each publisher has its own bounded rolling window;
station switches do not transfer cached data between streams.

The server does not alter nginx, systemd, or an existing single-radio
installation automatically. Use separate output paths and a distinct TCP port
for the OndaBaja service. Adapt `deploy/ondabaja-server.service` to your output
paths and install `deploy/ondabaja-nginx.conf` into your HTTPS site.
See [Linux deployment instructions](docs/DEPLOYMENT.md) for installation,
directory permissions, and private configuration.
