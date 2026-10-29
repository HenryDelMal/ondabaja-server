# ECNG model pairing

The native worker supports ECNG through `--codec ECNG`. The profile uses
24 kHz mono audio, two codebooks at 1.5 kbps or four at 3 kbps. The Python
worker command selects greedy RVQ (`--beam-size 1`) for this profile.
Station TOMLs select `codec = "ECNG"`; the encoder model path is
`model_dir/ecng-24khz-encoder-f32.bin`. ECNG segments use `.ecng` filenames
and model identifier `ecng_24khz`, retaining the existing ELTCP S framing.

Encoder and decoder weights must come from the matching export. A shared
filename or model identifier does not establish checkpoint compatibility.

The active export is the **ECNG training 4090 best checkpoint at step 1,870,000**. Its source SHA-256 is `bc933fad9eaee2f437cf23d3411890bdf2ef89d11d26cc73dc2148b61266fc14`. Encoder and decoder artifacts below were regenerated together from that exact checkpoint.

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| Server encoder FP32 | 31,791,080 | `6073d408c32389db1761e87fe7818dc8541d7028645f7c2845655d7c227a65c8` |
| Android decoder FP32 | 33,981,819 | `90519de8f4e80c7f1d957c51893b5ead14bdbfedd06e58556c7c0653189fbd69` |
| Android decoder FP16 | 16,992,703 | `8921cd95a871eea1bdd1336daa674bb5899c58f5583fbe0c0712210c93b008ff` |
| Android decoder INT8 | 10,049,239 | `f881e6845f85aff2e5793e049f68ffdfe4a1fc37fc3cb6d133c819449dee012b` |
| Android decoder INT4 | 6,081,367 | `d6791817bbfbb1617ba7565c60fdcc2d6bf048afc3266b0333fa495614ceda57` |

The encoder and decoder RVQ weights are checkpoint-specific and must be deployed as a matched set. The new encoder is ignored by Git and must be included in the server deployment package. Existing published segments created with older encoder weights must not be mixed with segments created by this checkpoint.

`stream_defaults.model_dir` in the root `server.toml` selects the shared model
folder. Future exports replace the conventional encoder filename and manifest;
client decoders must be updated to match the chosen export.
