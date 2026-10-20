# EnCodec Live Protocol v1

This project uses a deliberately small, HLS-shaped protocol. It is not HLS and
must not use an `.m3u8` content type: standard HLS players do not know EnCodec.
The manifest media type is `application/vnd.encodec.live+json`.

## Transport objects

`stream.json` is an atomically replaced, minified UTF-8 JSON document using
protocol version 1. The publisher also writes `stream.pb`, an atomically
replaced compact Protocol Buffers manifest using schema version 2. Both files
describe the same segment window and use `no-store` caching. The protobuf schema
is [`proto/stream.proto`](../proto/stream.proto).

The optional persistent TCP transport carries the identical protobuf bytes and
ECDC files. Its framing and client algorithm are defined in
[`TCP_PROTOCOL.md`](TCP_PROTOCOL.md); HTTP publication remains available alongside it.

The protobuf layout omits data clients can derive. `media_sequence` plus the
flattened order of segments gives each sequence number; the URI is
`segment-%012d.ecdc` using that sequence. `segment_duration_samples` is fixed
for all segments in the manifest. The ECDC header supplies sample rate, model,
channels, codebooks, and LM state, so the client computes duration in seconds
from the fixed sample count and the sample rate in the first segment header.
The ECDC header also supplies actual audio length. Epoch groups carry one
numeric `epoch_start_unix_ms` and a discontinuity flag for their first segment.
Each segment stores only byte length and CRC-32C (Castagnoli) of its complete
ECDC file. Every ECDC segment is independently decodable and has its own
initialization header; language-model entropy coding is disabled.

The JSON manifest has these fields:

| Field | Meaning |
| --- | --- |
| `format`, `version` | Fixed as `encodec-live-v1` and `1`. |
| `updated_at` | RFC 3339 UTC time at manifest publication. |
| `media_sequence` | Sequence of the first listed segment, or the next sequence when empty. |
| `discontinuity_sequence` | Number of discontinuity markers removed from the head of this rolling manifest. |
| `target_duration` | Configured fixed segment duration in seconds. Short final PCM tails are discarded. |
| `independent_segments` | Always `true`. |
| `init` | Stream-wide codec/container information shown below. |
| `segments` | Ordered rolling window of segment records. |

The files are atomically replaced individually. If a client compares both,
match their `media_sequence` and segment count. Protobuf schema version
2 is wire-incompatible with the earlier protobuf draft; clients should
regenerate bindings from the current schema and check `schema_version`.

Clients must ignore unknown manifest fields. Titles are no longer published in
either encoding. Protobuf field number 4 and its former name `title` are reserved;
schema version 2 is retained because removing an optional field is compatible
with existing v2 readers. Old TOML `title` settings are accepted with a warning
and ignored. Clients should use a locally configured display label.

`init` fixes ECDC v0, the selected model profile, sample rate, channels, ten
bits per codebook, configured bandwidth/codebooks, `language_model=false`, and
`self_initializing_segments=true`.

| `samplerate` | ECDC model | Layout | Bandwidth/codebooks |
| ---: | --- | --- | --- |
| `24` | `encodec_24khz` | 24,000 Hz mono | 1.5/2, 3/4, 6/8, 12/16, 24/32 |
| `48` | `encodec_48khz` | 48,000 Hz stereo | 3/2, 6/4, 12/8, 24/16 |

The 48 kHz model advances its internal one-second windows by 47,520 samples (0.99
seconds). Publishers should make independent outer segments an exact multiple
of that stride—normally 1.98, 2.97, 3.96, or 4.95 seconds. Other positive
durations remain valid protocol values, but an unaligned duration can create a
tiny final model frame and a more audible boundary seam. Version 1 recommends
3.96 seconds as the conservative default. The 24 kHz causal model advances one
codec frame every 320 samples (1/75 second), so its outer segments should be an
integer multiple of that interval. A 3.96-second segment is aligned for both
profiles.

Each segment record contains:

| Field | Meaning |
| --- | --- |
| `sequence` | Monotonically increasing integer; never reused in an output directory. |
| `uri` | Immutable numbered ECDC object. |
| `duration`, `sample_count` | Exact presentation duration and sample-frame count in the selected layout. |
| `pts_samples` | Zero-based presentation offset within `epoch`, in the selected profile's sample frames. |
| `program_date_time` | RFC 3339 UTC estimate anchored when this FFmpeg run starts. |
| `epoch` | UUID for one uninterrupted FFmpeg process/output timeline. |
| `epoch_start_unix_ms` | Numeric start timestamp used as the protobuf epoch-group identifier. |
| `discontinuity` | `true` on the first segment after service start or FFmpeg reconnect. |
| `byte_length`, `sha256` | Integrity and completeness checks for the ECDC object. |

FFmpeg raw PCM does not carry source PTS. Consequently `pts_samples` is exact
relative to captured PCM, while `program_date_time` is a server-clock estimate,
not recovered input PTS. A new epoch makes that loss of continuity explicit.

## Client algorithm

Poll the manifest without caching, initially select a segment a small number of
entries behind the live edge, then fetch segments by increasing sequence. For
JSON, check the byte length and optionally SHA-256. For protobuf, check the byte
length and CRC-32C. A missing expected sequence or an epoch/discontinuity change
requires flushing decoder/audio timing state and rebuffering. Do not concatenate
ECDC files and parse them as one ECDC file; open each segment independently and
keep one audio output sink alive across segment boundaries.

If the first available sequence is newer than the client's next sequence, the
client fell behind cleanup and must jump forward with a discontinuity. Poll at
roughly half `target_duration`; apply bounded retry/backoff when the manifest is
unchanged or the server is unavailable.

## Publication, cleanup, and restart

The writer buffers PCM until it has one full configured segment, dropping a
short final tail so every published segment has the same duration. It creates a
temporary file in the serving directory, optionally `fsync`s it, and renames it
over the final path. It publishes the segment before atomically replacing the
manifests. nginx therefore never sees a manifest that points at a partial
segment. The manifest retains `window_segments`; files remain
for an additional `stale_grace_segments` window to reduce races with clients
holding a recently replaced manifest.

At startup, the writer scans numbered segment names and resumes above the
largest sequence. It retains a compatible prior manifest window and marks the
first new segment discontinuous. A damaged/incompatible manifest is rebuilt;
sequence numbers still are not reused. Changing the codec initialization drops
the old manifest window and begins a new one. Orphan files are eventually
cleaned.

## HTTP caching

Serve both manifest files with `Cache-Control: no-store, max-age=0` and no
ETag. Serve `stream.pb` as `application/protobuf`. Serve
numbered segments with `Cache-Control: public, max-age=31536000, immutable` and
an ETag. Do not enable nginx directory indexes. TLS is strongly recommended for
traffic outside a trusted LAN.

## Why not one Icecast-style response?

Official ECDC v0 records total audio length in its opening header, so it cannot
represent an endless source as one valid file. A future custom continuous
transport could length-prefix complete ECDC segments on a chunked HTTP response,
but it would add reconnection/framing logic and still require a custom Android
client. HTTP uses ordinary static objects; the optional ELTCP transport requests
the same complete files through a persistent framed connection.
