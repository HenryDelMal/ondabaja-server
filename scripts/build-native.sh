#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR=${BUILD_DIR:-"$PROJECT_DIR/build/native"}
INSTALL_PREFIX=${INSTALL_PREFIX:-"$PROJECT_DIR"}
ENCODEC_CPP_DIR=${ENCODEC_CPP_DIR:-"$PROJECT_DIR/native/encodec"}
ENCODEC_CPP_UPDATE=${ENCODEC_CPP_UPDATE:-0}
BUILD_JOBS=${BUILD_JOBS:-1}

case "$BUILD_JOBS" in
    ''|*[!0-9]*|0)
        echo "BUILD_JOBS must be a positive integer" >&2
        exit 2
        ;;
esac

# CMake caches absolute source/build paths. Keep the managed upstream checkout,
# but discard only CMake-generated state when an installation was copied or
# moved to another directory.
if [ -f "$BUILD_DIR/CMakeCache.txt" ]; then
    CACHED_SOURCE=$(sed -n 's/^CMAKE_HOME_DIRECTORY:INTERNAL=//p' \
        "$BUILD_DIR/CMakeCache.txt")
    CACHED_BUILD=$(sed -n 's/^CMAKE_CACHEFILE_DIR:INTERNAL=//p' \
        "$BUILD_DIR/CMakeCache.txt")
    if [ "$CACHED_SOURCE" != "$PROJECT_DIR/native" ] || \
       [ "$CACHED_BUILD" != "$BUILD_DIR" ]; then
        echo "Discarding relocated CMake cache from ${CACHED_SOURCE:-unknown}" >&2
        cmake -E remove_directory "$BUILD_DIR/CMakeFiles"
        cmake -E remove \
            "$BUILD_DIR/CMakeCache.txt" \
            "$BUILD_DIR/Makefile" \
            "$BUILD_DIR/build.ninja" \
            "$BUILD_DIR/cmake_install.cmake" \
            "$BUILD_DIR/install_manifest.txt" \
            "$BUILD_DIR/rules.ninja" \
            "$BUILD_DIR/.ninja_deps" \
            "$BUILD_DIR/.ninja_log"
    fi
fi

if [ "$ENCODEC_CPP_UPDATE" = 1 ]; then
    ENCODEC_CPP_REVISION=$(
        "$PROJECT_DIR/scripts/update-encodec-cpp.sh" "$ENCODEC_CPP_DIR"
    )
elif [ ! -f "$ENCODEC_CPP_DIR/src/lib/encodec.cpp" ] && \
     [ ! -f "$ENCODEC_CPP_DIR/encodec.cpp" ]; then
    echo "ENCODEC_CPP_UPDATE=0 requires an encodec.cpp source at $ENCODEC_CPP_DIR" >&2
    exit 1
else
    ENCODEC_CPP_REVISION=$(git -C "$ENCODEC_CPP_DIR" rev-parse HEAD 2>/dev/null || echo unknown)
fi

mkdir -p "$BUILD_DIR"
printf '%s\n' "$ENCODEC_CPP_REVISION" > "$BUILD_DIR/encodec-cpp-revision.txt"
echo "Building with encodec.cpp revision $ENCODEC_CPP_REVISION"

cmake -S "$PROJECT_DIR/native" -B "$BUILD_DIR" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$INSTALL_PREFIX" \
    -DENCODEC_CPP_SOURCE_DIR="$ENCODEC_CPP_DIR"
cmake --build "$BUILD_DIR" --parallel "$BUILD_JOBS"
cmake --install "$BUILD_DIR"

echo "Installed native worker: $INSTALL_PREFIX/bin/encodec-live-native"
