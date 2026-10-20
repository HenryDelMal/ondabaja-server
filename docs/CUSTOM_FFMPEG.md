# Custom Linux FFmpeg with DeepFilterNet dialogue enhancement

`scripts/build-ffmpeg.sh` builds FFmpeg with SVT's `dnenhance` filter and
DeepFilterNet's native Rust `libdf` library. It installs the standard DFN3 ONNX
model and provides wrapper executables which locate their own private library.
This is optional server preprocessing; it does not change ECDC or the clients.

The default prefix is `/opt/encodec-live/ffmpeg`. The distro FFmpeg remains
available at its existing path. The streamer's `ffmpeg` TOML key selects the
executable to use.

## What is included

The default FFmpeg revision is the explicit release tag `n8.1.3`; the SVT filter
and DeepFilterNet default to their `main` revisions. Every run fetches those
refs and records the exact resolved commits in `current/build-info.txt`.

FFmpeg's usual codecs, formats, protocols, and built-in filters remain enabled.
This restores newer built-in audio filters missing from older distribution
versions, including `aemphasis`. The script also enables HTTPS via GnuTLS,
ALSA input, SoXR resampling, Opus/Vorbis/LAME/Speex libraries, and the optional
`bs2b`, `rubberband`, `sofalizer`, and `ladspa` filters. It checks a specific
inventory of useful audio filters before activating the build. Additional
external filters, such as ones needing Whisper or GPU libraries, need their
own dependencies and configure options; they are not all bundled here.

The resulting FFmpeg build enables GPL components. Source projects retain their
own licenses; copies are installed in `current/share/licenses`.

## Build on Debian or Ubuntu

Install a recent stable Rust toolchain with Cargo first. Old distribution Cargo
packages, especially on Debian 11, may be too old. Use the official instructions
at <https://rustup.rs/> and make sure `cargo --version` works in the shell which
will build FFmpeg. The script starts with Cargo's upstream lockfile but allows
Cargo to reconcile it if it is stale. The resulting lockfile is retained in the
revision-specific source cache and reused on later builds; dependencies are not
forcibly upgraded on every run. The script does not install or change Rust
automatically.

From the streamer repository on the Linux server:

```bash
cargo --version
./scripts/build-ffmpeg.sh --install-deps --jobs 1
```

`--install-deps` uses apt-get, with sudo when needed, for C build dependencies.
It does not install Rust. Building requires write access to the installation
prefix and the repository's `build/ffmpeg` directory. If building as an ordinary
user, create a writable installation prefix first or use a prefix you own:

```bash
./scripts/build-ffmpeg.sh --install-deps --prefix "$PWD/build/custom-ffmpeg" --jobs 1
```

After dependencies are installed, subsequent builds can omit `--install-deps`.
Other Linux distributions can install the matching development packages manually
and run the script without that option. NASM is needed for normal x86 assembly
support. Build output is cached under `build/ffmpeg`; one C/Rust build job is the
default. Rust compilation can still use substantial RAM and several gigabytes
of disk. Leave sufficient memory/swap available; reduce concurrent workloads
on small servers. `--jobs 1` controls compilation parallelism, not runtime
EnCodec or DeepFilterNet inference threads.

To pin all sources, or to choose another FFmpeg release:

```bash
FFMPEG_REF=n8.1.3 \
DNENHANCE_REF=FILTER_COMMIT_SHA \
DEEPFILTER_REF=DEEPFILTER_COMMIT_SHA \
./scripts/build-ffmpeg.sh --jobs 1
```

Replace the placeholder SHAs with commits you want to build. Compatibility of
other FFmpeg releases is not guaranteed: the downstream filter uses internal
FFmpeg APIs. Configuration or compilation failures leave the current release
active. The script never resets a user Git checkout; source caches and patched
trees belong to this build directory.

Additional configure options can be supplied after `--`, for example:

```bash
./scripts/build-ffmpeg.sh --jobs 1 -- --enable-libflite
```

Install the relevant development library first. Inspect
`build/ffmpeg/objects-*/ffbuild/config.log` if configuration fails.

## Checks and installation

Before activation, the script checks its required audio-filter inventory,
HTTPS and ALSA support, and runs a short synthetic mono signal through the
actual DFN3 filter and 24 kHz PCM output. This catches missing library symbols,
unreadable/invalid model files, and broken filter integration. It does not
measure real-time performance or enhancement quality on your station.

Each build installs into a new `releases/` directory. Only after those checks
pass does the script atomically switch `current` to the new release. Stable
`bin`, `lib`, and `share` symlinks follow `current`. Existing installations must
use this layout; the script refuses to overwrite unrelated files/directories
at those paths. Earlier releases are retained for rollback. Review disk space
after repeated builds; failed builds can leave inactive release/source folders.

The `ffmpeg` and `ffprobe` wrappers set `LD_LIBRARY_PATH` to their release's
private library directory, so systemd needs no additional environment setting.
Already running processes continue using their selected release; restart the
streamer when ready to use the new build.

```bash
/opt/encodec-live/ffmpeg/bin/ffmpeg -hide_banner -h filter=dnenhance
/opt/encodec-live/ffmpeg/bin/ffmpeg -hide_banner -filters
```

## Configure the streamer

Add this to your existing `[stream]` table; leave your station input/output and
other stream settings as appropriate:

```toml
ffmpeg = "/opt/encodec-live/ffmpeg/bin/ffmpeg"
samplerate = 24
bandwidth_kbps = 3
output_options = [
  "-af",
  "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=mono,dnenhance=model=/opt/encodec-live/ffmpeg/share/libdf/DeepFilterNet3_onnx.tar.gz:attenuation_limit=12:lookahead=2"
]
```

The filter accepts 48 kHz mono; FFmpeg then resamples to the stream's configured
24 kHz PCM. The standard installed DFN3 model needs `lookahead=2`. Do not select
`lookahead=0` for this model. The low-latency model is a separate artifact and
is not installed by this script. `attenuation_limit=12` is an illustrative
moderate starting point, not a station-specific preset. Compare recordings:
dialogue enhancement may suppress music and introduces additional CPU load.

If you already use an `-af` chain, combine these filters into that single chain
rather than adding a second `-af`. Restart your own systemd service after
editing its TOML. To bypass enhancement, remove `dnenhance` from the filter
chain; to return to distro FFmpeg, restore its executable path as well.

## Sources

- SVT filter, build instructions, and model/lookahead contract:
  <https://github.com/svt/ffmpeg-filter-dnenhance>
- DeepFilterNet library and model sources:
  <https://github.com/Rikorose/DeepFilterNet>
- FFmpeg source and filter documentation:
  <https://ffmpeg.org/download.html>, <https://ffmpeg.org/ffmpeg-filters.html>
