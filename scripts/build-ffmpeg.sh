#!/usr/bin/env bash
# Linux FFmpeg + SVT dnenhance + DeepFilterNet, installed beside the system FFmpeg.
set -Eeuo pipefail
umask 022

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
BUILD_DIR=${FFMPEG_BUILD_DIR:-"$PROJECT_DIR/build/ffmpeg"}
PREFIX=${FFMPEG_PREFIX:-/opt/encodec-live/ffmpeg}
BUILD_JOBS=${BUILD_JOBS:-1}
FFMPEG_REF=${FFMPEG_REF:-n8.1.3}
DNENHANCE_REF=${DNENHANCE_REF:-main}
DEEPFILTER_REF=${DEEPFILTER_REF:-main}
INSTALL_DEPS=0

usage() {
    cat <<'HELP'
Usage: build-ffmpeg.sh [--install-deps] [--prefix DIR] [--jobs N] [-- FFMPEG_OPTIONS...]

Build on Linux; requires a recent stable Rust/Cargo toolchain on PATH.
--install-deps  Install Debian/Ubuntu C build dependencies using apt-get.
--prefix DIR    Installation root (default /opt/encodec-live/ffmpeg).
--jobs N        Parallel C and Rust build jobs (default 1).
--help          Show this help without downloading or installing anything.

Environment: FFMPEG_BUILD_DIR, FFMPEG_PREFIX, BUILD_JOBS, FFMPEG_REF (n8.1.3),
DNENHANCE_REF (main), DEEPFILTER_REF (main). Refs can be branches, tags or SHAs.
Normal FFmpeg filters/codecs remain enabled. HTTPS, ALSA, SoXR, Opus, Vorbis,
LAME, Speex, bs2b, rubberband, libmysofa and LADSPA support are included.
Optional extra FFmpeg configure arguments can be passed after --; their
dependencies must already be installed. No configure arguments are evaluated
as shell code. Every build records its exact source revisions.

Each successful build is installed in PREFIX/releases and activated via
PREFIX/current. PREFIX/bin/ffmpeg and ffprobe wrap their private libdf library.
Previous installations are retained. No service is restarted automatically.
HELP
}

die() { echo "build-ffmpeg: $*" >&2; exit 1; }
trap 'echo "build-ffmpeg: failed at line $LINENO; previous active build was retained." >&2' ERR
while (($#)); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --install-deps) INSTALL_DEPS=1; shift ;;
        --prefix|--jobs)
            (($# >= 2)) || die "$1 needs a value"
            if [[ $1 == --prefix ]]; then PREFIX=$2; else BUILD_JOBS=$2; fi
            shift 2 ;;
        --) shift; break ;;
        *) die "Unknown option $1 (use --help)" ;;
    esac
done
[[ $(uname -s) == Linux ]] || die "This script targets Linux; run it on the streamer server."
[[ $BUILD_JOBS =~ ^[1-9][0-9]*$ ]] || die "BUILD_JOBS must be a positive integer"
[[ $PREFIX == /* && $PREFIX != / && $BUILD_DIR == /* && $BUILD_DIR != / ]] ||
    die "Prefix and build directory must be absolute directories other than /"
# Avoid whitespace/colon paths: FFmpeg configure and loader/filter paths have
# additional escaping rules which this script intentionally does not guess.
[[ $PREFIX != *[[:space:]:]* && $BUILD_DIR != *[[:space:]:]* ]] ||
    die "Prefix/build paths must not contain whitespace or colons"

if ((INSTALL_DEPS)); then
    command -v apt-get >/dev/null || die "--install-deps requires Debian/Ubuntu apt-get"
    APT=(apt-get)
    if ((EUID != 0)); then APT=(sudo apt-get); fi
    "${APT[@]}" update
    "${APT[@]}" install -y build-essential git pkg-config nasm python3 ca-certificates util-linux \
        libgnutls28-dev libasound2-dev libsoxr-dev libopus-dev libvorbis-dev \
        libmp3lame-dev libspeex-dev libbs2b-dev librubberband-dev libmysofa-dev ladspa-sdk
fi
for tool in git make cc pkg-config cargo tar install readlink; do
    command -v "$tool" >/dev/null || die "Missing $tool. See docs/CUSTOM_FFMPEG.md."
done
for library in gnutls alsa soxr opus vorbisenc speex libbs2b rubberband; do
    pkg-config --exists "$library" || die "Missing development library $library; use --install-deps"
done

mkdir -p "$BUILD_DIR" "$PREFIX/releases"
BUILD_DIR=$(CDPATH= cd -- "$BUILD_DIR" && pwd)
PREFIX=$(CDPATH= cd -- "$PREFIX" && pwd)
[[ $PREFIX != / && $BUILD_DIR != / ]] || die "Resolved prefix/build directory must not be /"
exec 9>"$BUILD_DIR/.build.lock"
command -v flock >/dev/null || die "Missing flock (install util-linux)"
flock -n 9 || die "Another build is using $BUILD_DIR"
# Refuse to replace files/directories belonging to another installation.
for entry in bin lib share; do
    if [[ -e $PREFIX/$entry || -L $PREFIX/$entry ]]; then
        [[ -L $PREFIX/$entry && $(readlink "$PREFIX/$entry") == "current/$entry" ]] ||
            die "$PREFIX/$entry is not managed by this script; choose a fresh prefix"
    fi
done
if [[ -e $PREFIX/current || -L $PREFIX/current ]]; then
    [[ -L $PREFIX/current && $(readlink "$PREFIX/current") == releases/* ]] ||
        die "$PREFIX/current is not a managed release symlink"
fi

# Clean cached Git repositories and revision-specific extracted sources avoid
# resetting any user's checkout and avoid reapplying patches to an old tree.
fetch_source() {
    local name=$1 url=$2 ref=$3 cache="$BUILD_DIR/cache/$1.git"
    [[ $ref != -* && -n $ref ]] || die "Invalid $name revision"
    if [[ ! -d $cache ]]; then git init --bare "$cache" >&2; fi
    echo "Fetching $name revision $ref" >&2
    git -C "$cache" fetch --depth=1 "$url" "$ref" >&2
    SOURCE_REV=$(git -C "$cache" rev-parse 'FETCH_HEAD^{commit}')
    SOURCE_DIR="$BUILD_DIR/sources/$name-$SOURCE_REV"
    if [[ ! -f $SOURCE_DIR/.extracted ]]; then
        # Extract to a new temporary directory, then publish the complete tree.
        [[ ! -e $SOURCE_DIR ]] || die "Incomplete source tree $SOURCE_DIR; move it aside before retrying"
        mkdir -p "$BUILD_DIR/sources"
        local temporary
        temporary=$(mktemp -d "$BUILD_DIR/sources/.extract-$name.XXXXXX")
        git -C "$cache" archive "$SOURCE_REV" | tar -xf - -C "$temporary"
        touch "$temporary/.extracted"
        mv "$temporary" "$SOURCE_DIR"
    fi
    echo "$name: $SOURCE_REV" >&2
}

fetch_source ffmpeg https://github.com/FFmpeg/FFmpeg.git "$FFMPEG_REF"
FFMPEG_SOURCE=$SOURCE_DIR; FFMPEG_REV=$SOURCE_REV
fetch_source dnenhance https://github.com/svt/ffmpeg-filter-dnenhance.git "$DNENHANCE_REF"
DNENHANCE_SOURCE=$SOURCE_DIR; DNENHANCE_REV=$SOURCE_REV
fetch_source deepfilter https://github.com/Rikorose/DeepFilterNet.git "$DEEPFILTER_REF"
DEEPFILTER_SOURCE=$SOURCE_DIR; DEEPFILTER_REV=$SOURCE_REV

MODEL="$DEEPFILTER_SOURCE/models/DeepFilterNet3_onnx.tar.gz"
[[ -f $MODEL ]] || die "Upstream model missing: $MODEL"
tar -tzf "$MODEL" > "$BUILD_DIR/model-contents.txt"
grep -q 'config.ini' "$BUILD_DIR/model-contents.txt" || die "Invalid model archive (possibly a Git LFS pointer)"

echo "Building DeepFilterNet with $BUILD_JOBS job(s); Rust compilation may take a while."
(
    cd "$DEEPFILTER_SOURCE"
    # Upstream main can ship a stale lockfile. Let Cargo reconcile it rather
    # than failing with --locked; later builds reuse this revision's lockfile.
    CARGO_TARGET_DIR="$BUILD_DIR/cargo-target" CARGO_PROFILE_RELEASE_DEBUG=0 \
        cargo build --lib --release --jobs "$BUILD_JOBS" \
        -p deep_filter --features capi,default-model,tract
)

PATCHED_SOURCE="$BUILD_DIR/patched/ffmpeg-$FFMPEG_REV-$DNENHANCE_REV"
if [[ ! -d $PATCHED_SOURCE ]]; then
    mkdir -p "$(dirname -- "$PATCHED_SOURCE")"
    cp -a "$FFMPEG_SOURCE" "$PATCHED_SOURCE"
fi
[[ -f $DNENHANCE_SOURCE/af_dnenhance.c ]] || die "Upstream dnenhance source missing"
cp "$DNENHANCE_SOURCE/af_dnenhance.c" "$PATCHED_SOURCE/libavfilter/af_dnenhance.c"
if ! grep -q 'CONFIG_DNENHANCE_FILTER' "$PATCHED_SOURCE/libavfilter/Makefile"; then
    printf '\nOBJS-$(CONFIG_DNENHANCE_FILTER) += af_dnenhance.o\n' >> "$PATCHED_SOURCE/libavfilter/Makefile"
fi
# The generated list references the symbol, so its declaration must precede
# that include. Also repair cached trees patched by earlier script versions.
grep -q '^#include "filters.h"' "$PATCHED_SOURCE/libavfilter/allfilters.c" ||
    die "Unsupported allfilters.c: missing filters.h include"
sed -i '/^extern const FFFilter ff_af_dnenhance;$/d' "$PATCHED_SOURCE/libavfilter/allfilters.c"
sed -i '/^#include "filters.h"/a extern const FFFilter ff_af_dnenhance;' \
    "$PATCHED_SOURCE/libavfilter/allfilters.c"

RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-${FFMPEG_REV:0:12}-$$"
RELEASE="$PREFIX/releases/$RELEASE_ID"
OBJECT_DIR="$BUILD_DIR/objects-$FFMPEG_REV-$DNENHANCE_REV"
mkdir -p "$OBJECT_DIR"
echo "Configuring FFmpeg with standard filters and additional audio libraries."
(
    cd "$OBJECT_DIR"
    "$PATCHED_SOURCE/configure" --prefix="$RELEASE" --bindir="$RELEASE/libexec" \
        --disable-shared --enable-static --disable-debug --disable-doc --disable-ffplay \
        --extra-libs=-ldl \
        --enable-gpl --enable-gnutls --enable-alsa --enable-libsoxr --enable-libopus \
        --enable-libvorbis --enable-libmp3lame --enable-libspeex --enable-libbs2b \
        --enable-librubberband --enable-libmysofa --enable-ladspa "$@"
    make -j "$BUILD_JOBS"
    make install
)
mkdir -p "$RELEASE/bin" "$RELEASE/lib" "$RELEASE/share/libdf" "$RELEASE/share/licenses"
install -m 0644 "$BUILD_DIR/cargo-target/release/libdf.so" "$RELEASE/lib/libdf.so"
install -m 0644 "$MODEL" "$RELEASE/share/libdf/DeepFilterNet3_onnx.tar.gz"
install -m 0644 "$DNENHANCE_SOURCE/LICENSE" "$RELEASE/share/licenses/dnenhance-LICENSE"
for license in LICENSE LICENSE-MIT LICENSE-APACHE; do
    if [[ -f $DEEPFILTER_SOURCE/$license ]]; then
        install -m 0644 "$DEEPFILTER_SOURCE/$license" "$RELEASE/share/licenses/deepfilter-$license"
    fi
done
for license in COPYING.GPLv2 COPYING.GPLv3 COPYING.LGPLv2.1 COPYING.LGPLv3 LICENSE.md; do
    if [[ -f $FFMPEG_SOURCE/$license ]]; then
        install -m 0644 "$FFMPEG_SOURCE/$license" "$RELEASE/share/licenses/ffmpeg-$license"
    fi
done
for program in ffmpeg ffprobe; do
    cat > "$RELEASE/bin/$program" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
program=$(basename -- "$0")
wrapper=$(readlink -f -- "$0")
release_dir=$(CDPATH= cd -- "$(dirname -- "$wrapper")/.." && pwd)
export LD_LIBRARY_PATH="$release_dir/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$release_dir/libexec/$program" "$@"
WRAPPER
    chmod 0755 "$RELEASE/bin/$program"
done
{
    printf 'ffmpeg=%s\ndnenhance=%s\ndeepfilter=%s\nbuild_jobs=%s\n' \
        "$FFMPEG_REV" "$DNENHANCE_REV" "$DEEPFILTER_REV" "$BUILD_JOBS"
    cargo --version
    cc --version
    "$RELEASE/bin/ffmpeg" -hide_banner -buildconf
} > "$RELEASE/build-info.txt" 2>&1

echo "Checking filters, input support and actual DeepFilterNet processing before activation."
"$RELEASE/bin/ffmpeg" -hide_banner -filters > "$RELEASE/filters.txt" 2>&1
for filter in dnenhance aemphasis aresample pan afftdn anlmdn arnndn loudnorm \
    alimiter asoftclip speechnorm dialoguenhance channelsplit sidechaincompress \
    rubberband bs2b sofalizer ladspa; do
    awk -v name="$filter" '$2 == name { found=1 } END { exit !found }' \
        "$RELEASE/filters.txt" || die "Required filter missing: $filter"
done
"$RELEASE/bin/ffmpeg" -hide_banner -protocols > "$RELEASE/protocols.txt" 2>&1
awk '$1 == "https" { found=1 } END { exit !found }' "$RELEASE/protocols.txt" ||
    die "HTTPS protocol missing"
"$RELEASE/bin/ffmpeg" -hide_banner -devices > "$RELEASE/devices.txt" 2>&1
grep -q ' alsa ' "$RELEASE/devices.txt" || die "ALSA device missing"
"$RELEASE/bin/ffmpeg" -hide_banner -loglevel error -nostdin \
    -f lavfi -i 'sine=frequency=440:sample_rate=48000:duration=0.1' \
    -af "aformat=sample_fmts=fltp:channel_layouts=mono,dnenhance=model=$RELEASE/share/libdf/DeepFilterNet3_onnx.tar.gz:attenuation_limit=12:lookahead=2" \
    -ac 1 -ar 24000 -c:a pcm_f32le -f null -

for entry in bin lib share; do
    if [[ ! -L $PREFIX/$entry ]]; then ln -s "current/$entry" "$PREFIX/$entry"; fi
done
ln -s "releases/$RELEASE_ID" "$PREFIX/.current-$RELEASE_ID"
mv -Tf "$PREFIX/.current-$RELEASE_ID" "$PREFIX/current"
echo "Activated $PREFIX/bin/ffmpeg"
echo "Model: $PREFIX/share/libdf/DeepFilterNet3_onnx.tar.gz"
echo "Revisions/filter inventory: $PREFIX/current/build-info.txt and filters.txt"
echo "Set the stream TOML ffmpeg path explicitly; restart the service when ready."
