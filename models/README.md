# ECNG encoder models

Current export: `ECNG training 4090 completed checkpoint at step 2,000,000`.

The active encoder is `ecng-24khz-encoder-f32.bin` (SHA-256
`6488da31661c0dfba2f2b3e3ce2e73ccfb56ed643be9e47137d66d949882e80b`).
`manifest.json` records the export and matching decoder checksums.

The root `server.toml` sets `stream_defaults.model_dir = "models"`.
To upgrade, stop the server, replace the active encoder and its manifest with
artifacts from the new export, and restart. Deploy the matching decoder to
clients. The checkpoint identifier is documentation, not hardcoded into the
loader; the active filename stays stable across model updates.

The previous bundled encoder is preserved in `archive/` (ignored by Git).
