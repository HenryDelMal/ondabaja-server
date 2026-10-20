# ECNG encoder models

Current export: `radio-finetune-v1-step-000100000`.

The active encoder is `ecng-24khz-encoder-f32.bin` (SHA-256
`cb3b155ce06fc90f7e44de3451dce229b927530d29b023d73e5be21f448dfb63`).
`manifest.json` records the export and matching decoder checksums.

The root `server.toml` sets `stream_defaults.model_dir = "models"`.
To upgrade, stop the server, replace the active encoder and its manifest with
artifacts from the new export, and restart. Deploy the matching decoder to
clients. The checkpoint identifier is documentation, not hardcoded into the
loader; the active filename stays stable across model updates.

The previous bundled encoder is preserved in `archive/` (ignored by Git).
