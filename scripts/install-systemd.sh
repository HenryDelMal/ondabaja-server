#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root (for example: sudo scripts/install-systemd.sh)" >&2
    exit 1
fi

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
INSTALL_DIR=/opt/encodec-live-streamer
PYTHON=${PYTHON:-python3}
PYTORCH_INDEX_URL=${PYTORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}
PYTORCH_PACKAGE=${PYTORCH_PACKAGE:-torch==2.4.1}
SERVICE_USER=encodec-live
SERVICE_GROUP=encodec-live

for command_name in "$PYTHON" ffmpeg cmake c++ getent groupadd systemctl useradd; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Required command not found: $command_name" >&2
        exit 1
    fi
done

if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    groupadd --system "$SERVICE_GROUP"
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --gid "$SERVICE_GROUP" --home-dir "$INSTALL_DIR" \
        --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -m 0755 "$INSTALL_DIR"
if [ "$SOURCE_DIR" != "$INSTALL_DIR" ]; then
    for directory in src config deploy docs scripts native tools models; do
        cp -a "$SOURCE_DIR/$directory" "$INSTALL_DIR/"
    done
    for file in pyproject.toml MANIFEST.in README.md LICENSE THIRD_PARTY_NOTICES.md Makefile; do
        install -m 0644 "$SOURCE_DIR/$file" "$INSTALL_DIR/$file"
    done
    chown -R root:root \
        "$INSTALL_DIR/src" "$INSTALL_DIR/config" "$INSTALL_DIR/deploy" \
        "$INSTALL_DIR/docs" "$INSTALL_DIR/scripts" "$INSTALL_DIR/native" \
        "$INSTALL_DIR/tools" "$INSTALL_DIR/models"
    chmod -R go-w \
        "$INSTALL_DIR/src" "$INSTALL_DIR/config" "$INSTALL_DIR/deploy" \
        "$INSTALL_DIR/docs" "$INSTALL_DIR/scripts" "$INSTALL_DIR/native" \
        "$INSTALL_DIR/tools" "$INSTALL_DIR/models"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 \
    "$INSTALL_DIR/.cache" \
    "$INSTALL_DIR/.cache/torch"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0755 \
    /opt/encodec-live \
    /opt/encodec-live/models \
    /opt/encodec-live/public

BUILD_DIR="$INSTALL_DIR/build/native" INSTALL_PREFIX="$INSTALL_DIR" \
    "$INSTALL_DIR/scripts/build-native.sh"

"$PYTHON" -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$INSTALL_DIR/.venv/bin/python" -m pip install "$INSTALL_DIR"

MODEL_DIR=/opt/encodec-live/models \
NATIVE_ENCODER="$INSTALL_DIR/bin/encodec-live-native" \
PYTHON="$PYTHON" PYTORCH_INDEX_URL="$PYTORCH_INDEX_URL" \
PYTORCH_PACKAGE="$PYTORCH_PACKAGE" \
TORCH_HOME="$INSTALL_DIR/.cache/torch" \
    "$INSTALL_DIR/scripts/prepare-models.sh"
chown -R root:root /opt/encodec-live/models
chmod -R go-w /opt/encodec-live/models

if [ ! -e /etc/encodec-live.toml ]; then
    install -o root -g "$SERVICE_GROUP" -m 0640 \
        "$INSTALL_DIR/config/encodec-live.example.toml" /etc/encodec-live.toml
fi
install -o root -g root -m 0644 \
    "$INSTALL_DIR/deploy/encodec-live.service" /etc/systemd/system/encodec-live.service

systemctl daemon-reload

echo
echo "Installation complete."
echo "1. Edit /etc/encodec-live.toml and replace the example input."
echo "2. Run: sudo -u $SERVICE_USER $INSTALL_DIR/.venv/bin/encodec-live check --config /etc/encodec-live.toml"
echo "3. Run: systemctl enable --now encodec-live"
echo "4. Optionally install deploy/nginx.conf after adapting its server/listen settings."
