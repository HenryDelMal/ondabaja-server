# Android EnCodec Player compatibility

This streamer now shares the portable Eigen-based C++ EnCodec core developed in
the **Build Android EnCodec decoder** project. The Linux side uses its encoder;
the Android player uses its decoder through JNI. Model files use the same native
format, although Linux needs combined encoder/decoder weights while the APK may
ship smaller decoder-only files.

## Codec and container compatibility

The current native Android implementation accepts both stream profiles:

- `encodec_24khz`, 24 kHz mono, 75 code frames/second, no normalization scale;
- `encodec_48khz`, 48 kHz stereo, 150 code frames/second, official one-second
  frames with per-frame scale and 1% overlap;
- ECDC container version 0 with raw 10-bit code indices and `lm=false`;
- independently initialized ECDC files for every live segment.

At 3 kbps, a 24 kHz stream carries four codebooks while a 48 kHz stream carries
two. Clients must trust and validate the ECDC `m`/`nc` metadata and manifest
`init`; they must not infer the model only from bitrate.

## Live transport expectations

The Android live implementation should:

1. Poll the JSON v1 manifest with caching disabled and resolve relative segment
   URLs against the manifest URL. The publisher also emits `stream.pb` with
   schema version 2 for clients using generated bindings from
   `proto/stream.proto`; the Android player currently consumes `stream.json`.
2. For protobuf, derive sequence and URL from `media_sequence` and segment
   order. Use its fixed `segment_duration_samples`; every listed segment has
   that duration. Validate byte length and CRC-32C before decode. JSON clients
   continue to use the per-segment URI and SHA-256 fields.
3. Read the first ECDC header to select and validate the native 24 kHz or 48 kHz
   decoder. The protobuf manifest intentionally has no separate `init` object.
4. Keep one decoder/audio sink alive while opening a fresh ECDC reader for each
   independently encoded segment.
5. Flush and rebuffer after a sequence gap or discontinuity. In protobuf, each
   `EpochGroup` describes one timeline and its flag applies to the first listed
   segment in that group.
6. Configure `AudioTrack` for 24 kHz mono or 48 kHz stereo from the ECDC header
   instead of assuming the HQ layout.

Titles are no longer published. Use a locally configured or URL-derived display
label. Existing protobuf v2 readers remain compatible with the reserved former
title field; JSON readers must tolerate its absence.

## Optional TCP with HTTP fallback

Implement ELTCP v1 from [`TCP_PROTOCOL.md`](TCP_PROTOCOL.md) using a persistent
socket and the same protobuf v2 bindings. The endpoint configuration must store
both a TCP host/port and its paired HTTPS manifest URL; no title or endpoint
discovery data is added to the manifest. TCP requests use the index in the last
manifest delivered on that connection; playback still tracks absolute uint64
sequence numbers across TCP reconnects and HTTP fallback.

Read a whole framed response before interpreting the next one: TCP read calls
can split or combine messages arbitrarily. Read protobuf sizes as unsigned
varints, cap allocation (for example 1 MiB for a manifest or segment), and check
schema version, sample duration, and ECDC header consistency. Use the manifest's
CRC32C for fetched segments, with no second checksum in the TCP envelope.

Allow a read timeout greater than the server's long-poll wait (default 15 s;
30 s is a suitable client default for that setting). On connection failure,
timeout, `b`, `r`, or unsupported framing, fetch the configured HTTPS
`stream.pb` URL, continuing from the next unplayed sequence where available.
Use `stream.json` only if protobuf is unavailable, validating SHA-256 there.
Back off subsequent TCP attempts (for example 30 s, doubling to at most 5 min)
while HTTP playback continues. TCP and HTTP must point at the same publisher;
deduplicate absolute sequences so a fallback does not replay audio.

To detect an idle/dead connection during `m`, the updated player must recognize
`h` as a one-byte heartbeat and continue waiting for the final `m` or `n` response.
Reset its liveness timer on each `h`; reconnect/fallback only when heartbeats
stop past the chosen timeout. Server heartbeats are opt-in
(`tcp_heartbeat_enabled`, default false) so older app builds do not see an
unknown response. Enable the server setting after deploying a client that
handles `h`; older app versions are incompatible with heartbeats while waiting.
The client may also send `h` between requests to check the connection; the
server immediately echoes `h`. Do not send that probe while waiting on `m`:
the server is long-polling, and unsolicited `h` responses already keep that
request alive.

The related Android task already contains C++ decoders and ECDC parsing for both
profiles. No Python, PyTorch, ExecuTorch, Flutter, or server model file is needed
on the phone.

## Boundaries

Independently encoded chunks reset neural context. The 48 kHz model still uses
its official overlap-add *inside* each ECDC file, but neither model carries
context across numbered files. Keep direct PCM concatenation as the baseline;
if a measured seam remains, apply a short stateful client de-clicker without
changing the declared timeline. Do not normalize each segment independently,
because gain jumps can create another boundary artifact.
