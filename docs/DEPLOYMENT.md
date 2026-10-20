# Linux deployment

This service uses the EnCodec Live Streamer ingest, native encoder and atomic
publication code as its base. The OndaBaja coordinator adds ECNG stations and
one shared ELTCP station-selection listener.

From your checkout (the systemd example assumes `/opt/ondabaja-server`):

```sh
sudo apt-get update
sudo apt-get install -y build-essential cmake python3-dev python3-venv
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
./scripts/build-native.sh
```

FFmpeg must be on PATH, or configured as an absolute path. The build script
uses the bundled native sources and installs `bin/encodec-live-native`.
`BUILD_JOBS=2 ./scripts/build-native.sh` allows two compiler jobs when memory
permits. The ECNG encoder must exist at `models/ecng-24khz-encoder-f32.bin`,
or configure a different `stream_defaults.model_dir`.

## Private settings

Create `server.local.toml` beside `server.toml` and copy your station files into
`config/stations.local/`. Both locations are ignored by Git:

```sh
cp -n server.toml server.local.toml
mkdir -p config/stations.local
```

Set `server.stations_dir = "config/stations.local"`, the deployment TCP port,
`server.output_root`, and optional `server.public_base_url` in the private
file. Keep real deployment addresses and ports out of tracked files. Match
`max_streams` to your station count and the encoders the host can sustain.
Every station requires `[station]` with `id` and `title`, and `[stream]` with
`input`; shared encoder settings inherit from the private global file.

With the example `output_root = "public"`, the files are:

```text
/opt/ondabaja-server/public/
  catalog.pb
  catalog.json
  .ondabaja-revision.json
  <station-id>/
    stream.json
    stream.pb
    segment-000000000000.ecng
```

No station `output_dir` overrides are needed. Nginx maps `/radios/` to this
directory in `deploy/ondabaja-nginx.conf`; set the private `public_base_url`
to that directory's complete HTTP(S) URL. Catalogs then appear at
`<public_base_url>/catalog.pb` and `catalog.json`, and station manifests at
`<public_base_url>/<id>/stream.json` and `stream.pb`. If you use a different
output root or URL prefix, adapt all Nginx aliases and systemd ReadWritePaths.
The TCP port is independent of the HTTP(S) URL and Nginx port.

## Filesystem permissions and systemd

The example unit runs as `ondabaja`. Create that account once if needed:

```sh
id ondabaja >/dev/null 2>&1 || sudo useradd --system --user-group --home-dir /opt/ondabaja-server --shell /usr/sbin/nologin ondabaja
sudo install -d -o ondabaja -g ondabaja -m 0755 /opt/ondabaja-server/public
```

Existing files under the output root must also be writable by `ondabaja`.
The service user needs read access to the checkout, private configuration,
station files and model, and execute access to the native encoder. Keep
private configuration files readable only by root and the service group,
for example `root:ondabaja` with mode `0640`, and private station directories
mode `0750`. Do not give Nginx access to those files.

Nginx needs traversal permission on `/opt` and `/opt/ondabaja-server`, plus
read/traversal access to `public` and station directories. Newly created
media files are mode `0644`; the example unit uses `UMask=0022`, yielding
readable station directories. If the repository directory has restrictive
permissions, grant traversal with an ACL (replace `www-data` with your Nginx
worker user):

```sh
sudo apt-get install -y acl
sudo setfacl -m u:www-data:--x /opt/ondabaja-server
sudo -u www-data test -x /opt/ondabaja-server/public
```

Nginx only needs read access to media. The supplied location whitelist exposes
catalogs, manifests and segment files, and rejects other `/radios/` paths,
including revision state. Add those locations to your existing HTTP(S) server
block, or adapt the standalone example. Validate Nginx before reloading it.

Validate the service with the same config and user that systemd will use:

```sh
sudo -u ondabaja /opt/ondabaja-server/.venv/bin/ondabaja-server check --config /opt/ondabaja-server/server.local.toml
sudo install -m 0644 deploy/ondabaja-server.service /etc/systemd/system/ondabaja-server.service
sudo systemctl daemon-reload
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl enable --now ondabaja-server.service
sudo journalctl -u ondabaja-server.service -f
```

`check` validates configuration, FFmpeg, and the native model loader; it does
not fetch audio or prove sustained real-time encoding. Confirm each station
publishes new segments after startup and test both TCP playback and HTTP
fallback with a compatible client. Restart the service after configuration
changes. Keep revision state when restarting or deploying a new checkout.
