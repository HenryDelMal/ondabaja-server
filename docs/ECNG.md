# ECNG encoder profile

This service can encode the jointly trained OndaBaja ECNG model as well as
official Meta EnCodec models. ECNG is a 24 kHz mono codec with two supported
profiles: 1.5 kbps (2 codebooks) and 3 kbps (4 codebooks). Its ECDC-compatible
segments advertise the model identifier `ecng_24khz` and use the `.ecng`
extension. The matching decoder weights are distributed with the OndaBaja
Android app; ordinary EnCodec decoders cannot decode ECNG tokens.

The checkpoint-specific FP32 encoder model is included at
`models/ecng-24khz-encoder-f32.bin`. The install script copies it into
`/opt/encodec-live-streamer/models` and `prepare-models.sh` validates it with
the native worker. The bundled `native/encodec` source understands the ECNG
model header and is used by default. Upstream updates are opt-in with
`ENCODEC_CPP_UPDATE=1`; confirm the selected revision supports ECNG before
using it.

Select the codec in the stream configuration:

```toml
[stream]
codec = "ECNG"
samplerate = 24
bandwidth_kbps = 3
```

Use `bandwidth_kbps = 1.5` for two codebooks instead. ECNG rejects 48 kHz and
other bitrates. The sample configuration
[`config/encodec-live.example.toml`](../config/encodec-live.example.toml)
shows a complete setup. `scripts/prepare-models.sh` prepares the bundled ECNG
model and the official EnCodec models so either codec can be selected by a
service configuration.

For an existing installation, rebuild/install the native worker and model
files before restarting the service:

```sh
sudo ./scripts/build-native.sh
sudo env MODEL_DIR=/opt/encodec-live/models \
  NATIVE_ENCODER=/opt/encodec-live-streamer/bin/encodec-live-native \
  ./scripts/prepare-models.sh
sudo -u encodec-live /opt/encodec-live-streamer/.venv/bin/encodec-live \
  check --config /etc/encodec-live.toml
sudo systemctl restart encodec-live
```
