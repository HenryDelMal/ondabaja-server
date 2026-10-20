# ELTCP v1: EnCodec Live over TCP

ELTCP v1 is an optional request/response transport over one persistent ordinary
TCP connection. It carries the existing protobuf v2 manifest and complete ECDC
files, with minimal framing. HTTP JSON/protobuf publication continues normally.
The server uses only Python's standard library and does not re-encode for TCP.
In addition to manifest-relative segment reads, ELTCP v1 supports direct
absolute-sequence fetches so a client can advance without downloading each new
manifest.

## Architecture

```text
FFmpeg -> PCM -> native EnCodec worker -> ManifestStore
                                            |
                           atomic ECDC + JSON + protobuf files
                                            |
                               immutable published snapshot
                                  /                   \
                          nginx / HTTPS        in-process TCP listener
                                  \                   /
                                client playback queue
```

Only the publisher writes the directory and rolling window. After both HTTP
manifest files have been atomically replaced, it installs an immutable snapshot
and wakes TCP manifest waiters. Each connection retains the last snapshot it
was sent; a segment index always refers to that snapshot, even when publication
continues. Network reads and writes occur in bounded client threads and never
hold the publication lock or block encoding. The snapshot's manifest bytes are
exactly the bytes in `stream.pb` for that publication.

There is one listener per publisher/stream. A connection cannot select a
different stream or supply a filesystem path. The default maximum is 32
clients; each caches at most one fetched segment for CRC retries. Snapshots
retain metadata, not entire historical segment windows. Disk cleanup retains
the configured grace window and can expire an old snapshot's files.

## Configuration and startup

All settings go in the existing `[stream]` table:

```toml
tcp_enabled = true
tcp_host = "0.0.0.0"
tcp_port = 9001
tcp_max_clients = 32
tcp_idle_timeout = 60.0
tcp_manifest_wait = 15.0
tcp_heartbeat_enabled = true
tcp_heartbeat_interval = 3.5
tcp_max_retries = 2
tcp_diagnostics = false
tcp_segment_cache_bytes = 8388608
```

Defaults: disabled, host `127.0.0.1`, port 9001. Bind to an interface address,
`0.0.0.0` for all IPv4 interfaces, or an IPv6 literal such as `::1` or `::`.
Whether an IPv6 wildcard also accepts IPv4 is OS-dependent. A DNS bind name uses
IPv4. Each service instance must use a distinct address/port pair. The
provided systemd unit can listen on an unprivileged port without extra socket
activation or capabilities. There is no separate TCP process or nginx location.

`tcp_manifest_wait` sets the maximum long-poll wait for both `m` and `f`
(default 15 seconds) and must be shorter than `tcp_idle_timeout`. The idle timeout
bounds socket reads/writes, not the stream lifetime: active connections may
continue indefinitely. Configure the client's manifest-response timeout above
`tcp_manifest_wait` plus network margin. The maximum extra CRC sends is
`tcp_max_retries` (zero disables retries). Listener bind failures fail service
startup rather than silently pretending TCP is enabled. Shutdown wakes waiters
and closes sockets; service restart requires clients to reconnect and initialize.

The optional heartbeat is disabled by default for compatibility. Enable it only
after clients handle `h` during both `m` and `f`. While waiting, the server sends
`h` every `tcp_heartbeat_interval` seconds until the requested manifest changes
or segment arrives, or the total `tcp_manifest_wait` expires. Publication
produces `m` or `S`; the unchanged deadline
produces `n`. The heartbeat interval does not extend the wait deadline. Set the
client read timeout above the heartbeat interval with network margin, and above
the full wait if heartbeats are disabled. Heartbeats do not imply audio publication.

The listener is plain TCP without encryption or authentication. Use a trusted
network or a separately configured TLS tunnel if needed. TCP failure or blocked
ports are handled by the client's paired HTTPS endpoint. HTTP publication is
not conditional on TCP being enabled.

## Startup path, cache and timing diagnostics

`i` returns the latest complete published snapshot immediately, including an
empty snapshot. It never enters the `m`/`f` publication wait. Reconnecting clients
must send `i` again to establish the current legacy index mapping, choose a safe
absolute sequence, then send `f` immediately. No startup `h` probe, success ACK,
or manifest refresh is required between consecutive successful `f` requests.

The protobuf payload was already serialized during publication. The complete
`m + length + protobuf` frame is now also prepared there. A short condition lock
captures the immutable snapshot pointer; serialization, encoding, filesystem
operations, and client writes all occur outside that lock. `TCP_NODELAY` is set
at accept, before handler dispatch and before any capacity or normal response.
Every response frame uses one `sendall`, which handles partial kernel writes.

When TCP is enabled, the publisher also prepares immutable `S` and legacy `s`
frames from the encoded bytes and the already computed CRC32C. The shared cache
is bounded by `tcp_segment_cache_bytes` (default 8 MiB, allowed 0 through 1 GiB)
**and** `window_segments + stale_grace_segments`. The byte budget counts both
complete frame copies, including their framing; oldest entries are evicted first.
A segment larger than the budget is served from disk. Set the budget to zero
to disable caching. Cache misses retain the existing file read and length check.
Startup preloads the compatible manifest's active segments before accepting
clients. Absolute fetches can also use grace segments whose metadata is known
to this publisher; a restart only restores metadata from the active manifest.

This budget describes the shared retained frame cache, rather than total process
RSS. Publication can temporarily hold the old and replacement snapshot; client
writes and the existing one-frame-per-client CRC retry cache can retain a frame
beyond cleanup. Idle clients keep only their legacy manifest/index metadata and
one retry frame, rather than pinning a historical shared cache. Disk cleanup and
retry limits are unchanged. Available `f` requests never sleep; genuinely future
sequences use condition notification, existing heartbeats, and the existing deadline.

Enable `tcp_diagnostics = true` to log JSON records prefixed `ELTCP timing` at INFO.
Connection acceptance records include a connection ID and UTC timestamp. Each
successful write, including a wait heartbeat, records:

| Field | Meaning |
| --- | --- |
| `connection_id`, `command`, `sequence` | Connection, request, and absolute sequence where applicable. |
| `request_bytes`, `response_bytes`, `response` | Complete request and individual response frame size/type. |
| `accepted_utc`, `write_start_utc`, `write_complete_utc` | UTC correlation timestamps. |
| `accept_to_command_ms` | Accept to receipt of the complete command, including its varint. For later commands this is connection age, not a new accept delay. |
| `snapshot_lock_wait_ms`, `snapshot_lookup_ms` | Accumulated condition lock acquisition and snapshot capture durations. |
| `manifest_cache_hit`, `manifest_preparation_ms` | Published manifest frame reuse; preparation is zero in the response path. |
| `segment_lookup_ms`, `segment_cache_hit` | Segment/frame lookup and whether the retained or retry frame was used. |
| `file_read_ms`, `frame_preparation_ms` | Disk fallback and frame assembly, zero on a frame cache hit. |
| `checksum_preparation_ms` | Zero: CRC32C was computed by the publisher and reused. |
| `publication_wait_ms` | Accumulated time in condition waits for an unpublished segment or unchanged manifest. |
| `write_ms` | Duration of the individual `sendall` call. |
| `command_to_write_complete_ms` | Complete command receipt to completion of this write, including publication waits and previous heartbeat writes. |
| `processing_ms` | The previous duration minus publication waits; includes socket backpressure and scheduling. |
| `diagnostics_dropped` | Cumulative records dropped when the diagnostic queue was full. |

Durations use the monotonic clock. Stage fields are diagnostic observations,
not necessarily additive CPU-time measurements. Write completion means the
bytes were handed to the local socket; it does **not** mean Android received or
decoded them. A blocked socket increases write time without holding the
publication lock. Record construction and nonblocking queue insertion happen
in the client thread; JSON formatting and log output use a separate daemon
thread. The queue holds at most 1024 records and drops new records when full,
so a slow journal or log handler cannot stall a response. Diagnostics default
to disabled and require no protocol change.

The development measurements used 12 synthetic 990-byte ECDC files, a 142-byte
manifest, and 200 reconnect/`i`/existing-`f` sessions on loopback. They measure
server stages separately from local transport and do not measure encoding
quality, Linux deployment load, or mobile transport/playback latency. No
benchmark is required to deploy these changes.

Measured on macOS, Python 3.14.7, with diagnostics enabled:

| Server duration | Median ms | p95 ms |
| --- | ---: | ---: |
| `i`: complete command to write completion | 0.0090 | 0.0269 |
| `f`: cached, complete command to write completion | 0.0081 | 0.0128 |
| `f`: uncached, complete command to write completion | 0.0307 | 0.0386 |
| Uncached segment file read | 0.0209 | 0.0290 |
| `i` snapshot lock acquisition | 0.000208 | 0.000459 |
| Cached `f` socket write | 0.003375 | 0.005209 |

Both available-response paths had zero publication wait. The separate future
fetch measured 83.81 ms publication wait and 0.210 ms remaining processing,
including heartbeat writes. Loopback medians were 0.105 ms for `i` and 0.062 ms
for cached `f`; these include local transport and thread scheduling. The original
code's earlier 200-session smoke measurement was about 0.110/0.108 ms, but used
a smaller one-segment manifest and is not an identical-workload comparison.
These measurements cannot attribute the reported mobile manifest delay to the
server or predict device startup improvement. Correlate the deployed server's
UTC records with fresh OndaBaja sessions after removing its startup echo probe.

## Integer encoding and framing

Commands and response types are single ASCII bytes, with **no newline**.
Unsigned integers use canonical unsigned LEB128, the same varint encoding as
protobuf uint64: seven low bits per byte, low groups first, high bit set when
another byte follows. Examples: 0 = `00`, 127 = `7f`, 128 = `80 01`,
300 = `ac 02`. Values are bounded to uint64 (at most 10 bytes); overflow and
noncanonical encodings are protocol errors. These are binary integers, not
decimal text. The sequence itself is uint64; a uint32 fixed-width request is
unnecessary.

Manifest and legacy manifest-relative segment responses have this envelope:

```text
type: 1 byte | payload_length: unsigned varint | payload: exactly length bytes
```

Status responses contain only their one type byte. The absolute-sequence
response includes the requested segment's CRC32C and discontinuity flag because
the client may not have fetched a newer manifest. Manifest CRC32C covers each
entire ECDC file, including its header. Decode protobuf `fixed32 crc32c` as an
unsigned 32-bit value; its four wire bytes are little-endian. The CRC in an
absolute-sequence response is four-byte big-endian (network order). Transport
lengths and requested sequences are unsigned varints, not decimal text or
fixed-width integers.

TCP does not preserve message boundaries. A read may contain half a frame or
several frames. Use a buffered reader, decode one varint, then read exactly its
payload length; preserve any extra bytes for the next response. Before
allocating, enforce a client size limit (1 MiB for each manifest or segment is
ample for the provided profiles/configuration). For segment replies, require
the envelope length to equal the requested manifest entry's `byte_length`.
Use one outstanding response-producing request at a time; no request IDs are
needed and responses always correspond to the pending request.

## Client commands

| Bytes | Request | Server behavior |
| --- | --- | --- |
| `i` | Initialize/reset connection | Immediately sends `m` plus the latest protobuf manifest; establishes the index mapping. Required first command. |
| `m` | Ask for a changed manifest | Sends a new `m` frame immediately if changed, otherwise waits up to `tcp_manifest_wait`; replies `n` if still unchanged. |
| `s` + varint index | Fetch a segment | Index is zero-based in the flattened segment list of the last manifest sent on this connection. Sends `s` with raw ECDC or `g`. |
| `f` + varint sequence | Fetch by absolute sequence | Sequence is the global ECDC sequence number, independent of the last manifest. The server waits up to `tcp_manifest_wait` for a not-yet-published sequence, with optional `h` heartbeats; it sends `S` when available, `n` at the deadline, or `g` if stale/missing. |
| `e` | CRC error after a complete segment response | Resends the cached last segment with its original framing (`s` or `S`), up to the retry limit. |
| `o` | Optional success ACK | Frees the retry cache. No response. |
| `h` | Client-initiated liveness probe | Server immediately echoes one byte `h`. Send only between requests, not while awaiting `m` or `f`. |
| `q` | Close | Closes the connection. No response. Ordinary socket close also suffices. |

A new `s`, `f`, `i`, or `m` implicitly acknowledges the previous segment and clears
its retry cache. Avoid `o` for minimum traffic. Do not send `e` after asking for
another segment/manifest or acknowledging with `o`; it is then a protocol error.

`m` compares actual protobuf bytes against the last delivered manifest;
timestamps in JSON do not cause false changes. An `n` reply preserves the index
mapping. Every delivered `m` frame replaces it. `i` can request an immediate
refresh even if unchanged. An initial empty manifest is valid: it has no epoch
groups, and `media_sequence` is the next sequence to publish. Then request `m`
until segments are available. An unchanged finite/stalled source returns `n`
after each long-poll deadline, rather than spinning or claiming end-of-stream.
For an explicit probe, send `h` only when no other response is pending and
expect one `h` byte. While `m` or `f` is pending, do not send commands; treat server
`h` bytes as heartbeats until the final response arrives.

## Server responses

| Byte | Payload | Meaning / client action |
| --- | --- | --- |
| `m` | varint length + protobuf v2 | Replace the connection's manifest/index mapping. |
| `s` | varint length + complete ECDC | Validate length and manifest CRC32C, then decode. |
| `S` | varint length + CRC32C (4-byte big-endian) + flags (1 byte) + complete ECDC | Reply to `f`. Check ECDC length and CRC32C. Bit 0 of flags means this segment begins a discontinuity; all other bits are reserved and must be zero. |
| `n` | None | For `m`, the manifest was unchanged after the wait. For `f`, that sequence is not published yet; retry it later. |
| `h` | None | During `m` or `f`, a server heartbeat; keep waiting and reset the client's liveness timer. Also the echo for a client `h` probe sent between requests. |
| `g` | None | Requested segment is stale/unavailable or its file is missing/inconsistent; refresh with `m`/`i` and rebuffer if behind. |
| `b` | None, followed by close | Client capacity exhausted; use HTTP/backoff. May arrive before init. |
| `p` | None, followed by close | Protocol violation (unknown command, missing init, malformed varint, retry without a cached segment). |
| `r` | None, followed by close | CRC retry limit exhausted; reconnect or use HTTP. |

For `f`, `n` means the requested absolute sequence is not published yet; retry
the same sequence immediately: the server already waited for the configured
deadline. `f` is a single request/response (no
manifest is needed between sequential segment requests). On startup or after
reconnect, use `i` to obtain the current window and choose a starting absolute
sequence; then increment it after each `S` response. Refresh with `m` when
starting playback again, after `g`, or when the client needs a fresh live-edge
position. The `S` response carries CRC32C and discontinuity metadata so the
client can validate and resynchronize without a manifest refresh.

Socket failures/timeouts may close the connection without a status. A protocol
error status is best effort; a peer that sent unread invalid bytes may observe
a TCP reset. Receive EOF/reset at any point as connection loss. If a response
is truncated or framing/length is invalid, **reconnect**: an `e` request cannot
repair a desynchronized byte stream. Use `e` only after reading one full,
correctly framed segment whose CRC does not match.

The retry cache survives disk cleanup for that fetched segment and preserves
the exact response format (`s` or `S`). A repeated CRC
failure is bounded; retrying cannot fix an actually damaged file. Validate CRC
again on each retry. TCP itself already retransmits lost/corrupted packets and
delivers ordered bytes; application ACKs are optional, and no retry is needed
merely because an individual socket read returned fewer bytes than requested.

## Protobuf schema

Source of truth: [`../proto/stream.proto`](../proto/stream.proto).
Generate client bindings using that file; the schema is shared by HTTP and TCP:

```proto
syntax = "proto3";
package encodec.live.v2;

message StreamManifest {
  uint32 schema_version = 1; // Must be 2.
  uint64 media_sequence = 2;
  uint32 segment_duration_samples = 3;
  reserved 4;
  reserved "title";
  repeated EpochGroup epochs = 5;
}

message EpochGroup {
  uint64 epoch_start_unix_ms = 1;
  bool first_segment_discontinuity = 2;
  repeated Segment segments = 3;
}

message Segment {
  uint32 byte_length = 1;
  fixed32 crc32c = 2;
}
```

| Variable | Meaning |
| --- | --- |
| `schema_version` | 2; validate before using the manifest. ELTCP framing version is separately defined as v1 by this document/command set. Future incompatible framing needs a new initialization command; `i` retains v1 meaning. |
| `media_sequence` | Absolute uint64 sequence of flattened segment index 0. Sequence of index `k` is `media_sequence + k`; reject overflow. |
| `segment_duration_samples` | Fixed audio sample-frame count for every segment. Duration in seconds is this divided by the ECDC model's sample rate. |
| `epochs` | Ordered groups of consecutive segments from the same capture timeline. Flatten them in wire order for request indices. |
| `epoch_start_unix_ms` | Server-clock Unix milliseconds at the start of that capture timeline; identifies discontinuities/restarts. |
| `first_segment_discontinuity` | Applies to the first listed segment in that group. Other segments in the group are continuous. An epoch change also requires resynchronization. |
| `byte_length` | Number of bytes in the entire ECDC object; check against the TCP envelope or HTTP response. |
| `crc32c` | Unsigned Castagnoli CRC32C of the whole ECDC object. Do not use IEEE CRC32. Known vector: ASCII `123456789` -> `0xe3069283`. |

Titles are removed from both JSON and protobuf. Field 4/name `title` stay
reserved and schema v2 stays compatible with older optional-title readers.
Sample rate, model, channels, codebooks, audio length, and LM flag come from
ECDC; no `init`, per-segment `sample_count`, or second checksum is transmitted
in protobuf/TCP. Reject unsupported ECDC versions/models, LM data, or a header
audio length inconsistent with `segment_duration_samples`.

## Client scheduling and HTTP fallback

1. Store a TCP host/port **and** paired HTTPS manifest URL in client settings.
   They must refer to the same publisher. No manifest discovery fields are added.
2. Connect with a bounded connection timeout (for example 3 seconds), send `i`,
   then read one manifest frame. A blocked port or failure should promptly use
   HTTPS. On a new connection, old indices have no meaning until init completes.
3. Flatten epochs and initially choose a few segments behind the live edge for
   buffering. Track the absolute sequence number to play, not only an index.
4. Send `f` plus the varint absolute sequence for each segment. Validate the
   returned length, CRC32C, flags, ECDC header, and fixed duration; decode each
   complete ECDC independently while retaining the audio sink/queue. On CRC
   failure, send `e`; after success, increment the sequence and send `f` again.
   On `h`, keep waiting. On `n`, immediately retry the same sequence; the server
   already waited up to `tcp_manifest_wait`.
5. If the server returns `p` to `f`, it predates this extension. Close that
   connection, reconnect, and use the legacy `m`/`s` flow. Legacy `m` waits up
   to `tcp_manifest_wait`; its read timeout must exceed that duration plus
   network margin (30 seconds suits the default 15-second wait).
6. Refresh with `m` at startup/reconnect, after `g`, or when repositioning at
   the live edge. A segment sequence lower than `media_sequence` has aged out:
   choose a suitable live-edge sequence, flush/rebuffer, and honor epoch changes
   and the `S` discontinuity flag. Repeated `g` with an unchanged manifest
   should back off or use HTTP, not spin.
7. On TCP loss, `b`, `r`, or unsupported protocol, fetch the paired HTTPS
   `stream.pb`, using the same schema, CRC, and next absolute sequence.
   For HTTP, derive the URL as `segment-%012d.ecdc`, resolved relative to the
   HTTPS manifest URL. If protobuf is unavailable, use JSON/its URI and SHA-256.
   Discard duplicate already-played sequences. If the next sequence is absent
   due to cleanup or restart, rebuffer at the live edge.
8. While HTTP continues playing, retry TCP with bounded backoff (e.g. 30 s,
   then double up to 5 min), reinitialize, and resume by absolute sequence.
   A server restart marks new segments discontinuous. Model/layout changes
   require rebuilding the decoder/audio sink. No Android source is changed here.

For unsigned protobuf fields on Android, avoid accidentally treating CRC32C as
a signed negative number: compare masked 32-bit values. Likewise preserve uint64
sequence bits or explicitly reject values outside the client's supported
positive range. Use a Castagnoli implementation supported by your minimum
Android API or provide a portable implementation.

## Example exchange and overhead

```text
C -> S: 69                         # 'i'
S -> C: 6d <varint N> <N protobuf bytes>
C -> S: 73 00                      # 's', index 0
S -> C: 73 b2 03 <434 ECDC bytes>   # length 434 = b2 03
C -> S: 73 01                      # next index, implicit success ACK
S -> C: 73 b2 03 <434 ECDC bytes>
C -> S: 65                         # optional 'e' if this whole frame fails CRC
S -> C: 73 b2 03 <same 434 bytes>
C -> S: 6d                         # 'm' after the available window is fetched
S -> C: 6e                         # 'n' after long-poll timeout if unchanged
C -> S: 6d
S -> C: 6d <varint N> <new manifest> # publication changed; indices reset
```

With absolute fetch, after `i` provides the starting window the normal sequence
is instead:

```text
C -> S: 66 00                            # 'f', absolute sequence 0
S -> C: 53 b2 03 <CRC32C:4B big-endian> <flags:1B> <434 ECDC bytes>
C -> S: 66 01                            # next absolute sequence
S -> C: 6e                               # 'n': not published yet; retry 'f 01' later
```

For a one-byte sequence, an `f` request is two bytes. The ready reply adds eight
bytes before the ECDC (type, two-byte length, CRC32C, flags); the not-ready
exchange is three application bytes total. All totals exclude TCP/IP packets,
TLS, and socket-level acknowledgements.

An index under 128 costs one varint byte: two request bytes total. A 434-byte
segment costs three response framing bytes (type + two-byte length), so fetching
it costs five application overhead bytes, excluding TCP/IP packets and optional
ACK/retry. A small unchanged-manifest cycle costs two application bytes (`m`,
`n`) after the wait. Protobuf with eight 434-byte entries in one epoch is
approximately 97-103 bytes depending on sequence/discontinuity values; no title
is included. TCP framing adds two bytes when that manifest is under 128 bytes.
Network packet headers, transport ACKs, setup, and any separate TLS tunnel still
exist; these numbers describe application bytes only.

## Verification

`make test` includes loopback integration tests for split/coalesced commands,
protobuf and ECDC framing, stable indices during rollover, expired files,
cached retries after cleanup, retry limits, long polling, malformed commands,
capacity rejection, and shutdown. Tests use synthetic ECDC headers/payloads;
they require no native encoder or model downloads. Native decoding and Android
fallback/playback integration need separate testing with the client.
