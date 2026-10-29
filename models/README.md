# ECNG encoder models

Current export: `ECNG best checkpoint 1,870,000 from ECNG training 4090`.

The active encoder is `ecng-24khz-encoder-f32.bin` (SHA-256
`6073d408c32389db1761e87fe7818dc8541d7028645f7c2845655d7c227a65c8`).
`manifest.json` records the export and matching decoder checksums.

The root `server.toml` sets `stream_defaults.model_dir = "models"`.
To upgrade, stop the server, replace the active encoder and its manifest with
artifacts from the new export, and restart. Deploy the matching decoder to
clients. The checkpoint identifier is documentation, not hardcoded into the
loader; the active filename stays stable across model updates.

The previous bundled encoder is preserved in `archive/` (ignored by Git).
