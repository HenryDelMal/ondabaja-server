# ECNG model pairing

The native worker supports ECNG through `--codec ECNG`. The profile uses
24 kHz mono audio, two codebooks at 1.5 kbps or four at 3 kbps. The Python
worker command selects greedy RVQ (`--beam-size 1`) for this profile.
Station TOMLs select `codec = "ECNG"`; the encoder model path is
`model_dir/ecng-24khz-encoder-f32.bin`. ECNG segments use `.ecng` filenames
and model identifier `ecng_24khz`, retaining the existing ELTCP S framing.

Encoder and decoder weights must come from the matching export. A shared
filename or model identifier does not establish checkpoint compatibility.

The active export is the **ECNG training 4090 completed checkpoint at step 2,000,000**. Its source SHA-256 is `cb37b0efc647f52ee7db269490be51e4734d925c4c78b1f332729a0b361e847f`. This is the final resume checkpoint; its stored `best_validation` value records the earlier best at step 1,870,000 and is not a score for these final-step weights. Encoder and decoder artifacts below were regenerated together from the step-2,000,000 model state.

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| Server encoder FP32 | 31,791,080 | `6488da31661c0dfba2f2b3e3ce2e73ccfb56ed643be9e47137d66d949882e80b` |
| Android decoder FP32 | 33,981,819 | `e7c3334922103a61482bb3e9b46dbfa9ff0692cb1723bff3121f9dd201a65a45` |
| Android decoder FP16 | 16,992,703 | `4dce030e612bd360ecb30435bde8708764fbfa239f30e52343bce90e2b3dbf80` |
| Android decoder INT8 | 10,049,239 | `c1654d8fa913a5904a8147a2aa3546342ee548c1414e71914a40d90eee9eccb9` |
| Android decoder INT4 | 6,081,367 | `b7150d9dceb9009cf1f529ff1d4450d66802fb1c619b83b475ae8fcd0f361e6e` |

The encoder and decoder RVQ weights are checkpoint-specific and must be deployed as a matched set. The new encoder is ignored by Git and must be included in the server deployment package. Existing published segments created with older encoder weights must not be mixed with segments created by this checkpoint.

`stream_defaults.model_dir` in the root `server.toml` selects the shared model
folder. Future exports replace the conventional encoder filename and manifest;
client decoders must be updated to match the chosen export.
