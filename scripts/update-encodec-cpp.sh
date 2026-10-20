#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 DESTINATION" >&2
    exit 2
fi

destination=$1
repository=${ENCODEC_CPP_REPOSITORY:-https://github.com/HenryDelMal/encodec.cpp.git}
revision=${ENCODEC_CPP_REF:-main}
parent=$(dirname -- "$destination")

mkdir -p "$parent"

if [ -d "$destination/.git" ]; then
    if [ ! -f "$destination/.encodec-live-managed" ]; then
        echo "Refusing to modify an unmanaged Git checkout: $destination" >&2
        echo "Use an empty destination, or set ENCODEC_CPP_UPDATE=0." >&2
        exit 1
    fi
    echo "Checking $repository for encodec.cpp revision $revision" >&2
    git -C "$destination" remote set-url origin "$repository"
    git -C "$destination" fetch --quiet --depth 1 origin "$revision"
    git -C "$destination" checkout --quiet --detach --force FETCH_HEAD
elif [ -e "$destination" ]; then
    echo "encodec.cpp destination exists but is not a Git checkout: $destination" >&2
    exit 1
else
    temporary=$(mktemp -d "$parent/.encodec.cpp.XXXXXX")
    cleanup() {
        rm -rf -- "$temporary"
    }
    trap cleanup EXIT HUP INT TERM

    echo "Downloading $repository revision $revision" >&2
    git -C "$temporary" init --quiet repository
    git -C "$temporary/repository" remote add origin "$repository"
    git -C "$temporary/repository" fetch --quiet --depth 1 origin "$revision"
    git -C "$temporary/repository" checkout --quiet --detach FETCH_HEAD
    : > "$temporary/repository/.encodec-live-managed"
    mv "$temporary/repository" "$destination"
    rmdir "$temporary"
    trap - EXIT HUP INT TERM
fi

for required in src/lib/encodec.cpp src/lib/encodec.h LICENSE; do
    if [ ! -f "$destination/$required" ]; then
        echo "Required upstream file is missing: $required" >&2
        exit 1
    fi
done

git -C "$destination" rev-parse HEAD
