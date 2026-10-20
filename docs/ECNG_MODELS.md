# ECNG model pairing

The native worker supports ECNG through `--codec ECNG`. The profile uses
24 kHz mono audio, two codebooks at 1.5 kbps or four at 3 kbps. The Python
worker command selects greedy RVQ (`--beam-size 1`) for this profile.
Station TOMLs select `codec = "ECNG"`; the encoder model path is
`model_dir/ecng-24khz-encoder-f32.bin`. ECNG segments use `.ecng` filenames
and model identifier `ecng_24khz`, retaining the existing ELTCP S framing.

Encoder and decoder weights must come from the matching export. A shared
filename or model identifier does not establish checkpoint compatibility.

The EnCodec Live Streamer chat records deployment of the following export on
October 20, 2026: `radio-finetune-v1-step-000100000`.

| Artifact | SHA-256 |
| --- | --- |
| Encoder FP32 | `cb3b155ce06fc90f7e44de3451dce229b927530d29b023d73e5be21f448dfb63` |
| Android decoder FP16 | `77c56a4e2f7b3b7b2eacbc6cfc4c3286726d387444ed21972e7190cde5355918` |
| Android decoder INT8 | `bd3b9b2a9105ddaf46fc592a4733b91b8859fba84360d330b673780390a23549` |

The active encoder in this checkout is now the export listed above. Its
source manifest is preserved in `models/manifest.json`. The previous bundled
encoder (`5e2555dd08c18c114e1611777a1bc8fa337552279bcafb88de93d013e65a3f77`)
is retained under `models/archive/`.

`stream_defaults.model_dir` in the root `server.toml` selects the shared model
folder. Future exports replace the conventional encoder filename and manifest;
client decoders must be updated to match the chosen export.
