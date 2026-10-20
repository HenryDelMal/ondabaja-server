# OndaBaja service protocol

`stream.proto` remains the existing per-station EnCodec Live manifest schema.
`station_catalog.proto` defines the catalog sent over the OndaBaja-only TCP
listener. The client starts by sending `C` plus its last-seen catalog revision
(zero when it has no catalog). The server replies with a framed `c` catalog or
`n` when that revision is still current. Existing ELTCP v1 remains a separate listener with unchanged
`i/m/s/f` behavior.

All integers use the canonical uint64 protobuf/ULEB128 varint format. Catalog
and manifest payloads use `type + varint length + payload`. Segment responses
reuse the existing absolute-sequence `S` frame from `docs/TCP_PROTOCOL.md`.
Commands are:

| Request | Payload | Result |
|---|---|---|
| `C` | last catalog revision varint | `n` if equal, otherwise framed `c` catalog |
| `R` | varint byte length + UTF-8 stable station ID | framed `m` manifest; switches this socket |
| `B` | positive selection ID varint + station-ID byte-length varint + UTF-8 station ID + startup-lookback milliseconds varint | bundled manifest and first startup segment, or `n`; switches this socket |
| `M` | none | current selected station's framed `m` manifest |
| `f` | absolute sequence varint | `S`, `g`, or `n` using existing ELTCP v1 rules |
| `e` | none | retry the previous complete `S` frame |
| `H` | none | `h` liveness echo |
| `Q` | none | close |
| server `u` | none | unsolicited catalog/manifest-changed notification; no payload |

While `f` is waiting for a future sequence, the server continues checking the
socket. An `R` request cancels that wait and immediately returns the newly
selected station's manifest. In that case there is no `S` response for the old
fetch. A client must read framed response types and allow `m` to supersede a
pending `f`; it must then discard the old station's compressed queue, expected
sequence, retry state, and queued PCM before restarting from the new manifest.
`H` is also accepted during a pending fetch and is answered with `h`.
`B` selects a station and returns its startup data in one response. The reply
is `B` + the same selection-ID varint + manifest byte-length varint + manifest protobuf, followed by either
`n` when the manifest has no playable segment, or `S` + absolute-sequence
varint + the existing segment body (segment byte-length varint, four-byte
big-endian CRC32C, one flags byte, and the exact ECNG bytes). There is no
additional `S` marker before the segment length. For an unknown station the
reply is the single byte `x`. A client may retry the same `B` request with the
same selection ID on the same open socket. The server recognizes a retry while
the response is being prepared and lets the pending response satisfy it. If a
retry arrives after that response was sent, it is a new request and its reply
contains the same selection ID; clients can ignore already-consumed IDs.
Selection IDs are positive uint64 values echoed to correlate replies with the
current station-selection attempt.

The startup segment is chosen from the same immutable manifest snapshot in the
response. Starting at the newest segment, walk backward until accumulated
segment durations reach `max(1, startupLookbackMs)`. When the manifest has at
least eight segments, choose index 2 or later, leaving the two oldest segments
unused as the client's deletion margin. Smaller windows use all available entries.
This matches the Android `LiveSequenceTracker` startup selection algorithm.
An empty manifest returns the manifest followed by `n`.

Like `R`, `B` preempts a pending `f` on the same socket. Once the server has
read the complete `B` request, the bundled response supersedes the pending
fetch; no `S` or `n` response for that old fetch is sent. The server builds the
whole bundle before writing it, so its manifest and segment framing remain
contiguous as one response.

After a successful configuration reload, the server sends `u` to every
currently connected client. It may arrive while a client is waiting for a
segment or while the connection is otherwise idle. If it interrupts `f`, that
fetch has no segment response. The client should request `C` with its cached
revision, then send `R` with its currently selected station ID to receive the
latest manifest on the same TCP connection. If that station was removed, `R`
returns `x`; the client should keep the socket and let the user select another
catalog entry. The notification is a hint; the catalog revision remains the
authoritative change detector.

The request stream has one active station and one outstanding fetch per
connection. The client must not send concurrent `f` requests. Catalog revision
is monotonic and persisted in `<output_root>/.ondabaja-revision.json` by default;
an explicit `server.revision_file` can override this. Catalog changes include
ID/title/order/URL changes and edits to a station stream configuration. The UTC
millisecond field is informational. IDs should remain unchanged when only the
display title or configuration changes.

The catalog is generated from flat TOML files under server.stations_dir.
Each file is flat TOML with title and stream settings; id defaults to its
filename without .toml. The protobuf title field uses the same field number (2)
as the prototype name field. ECNG segments retain the ELTCP S framing.

The mux listener is plain TCP with no authentication or encryption. Expose it
only on a trusted network or tunnel. Station HTTP manifests and segments remain
available for older clients and as fallback.

HTTP publication uses the shared `server.output_root`: `catalog.pb` is the raw
StationCatalog protobuf payload (without the TCP `c` tag or length prefix),
and `catalog.json` is a readable equivalent. Their revision, timestamp and
station metadata match the TCP catalog for that service run. Per-station files
live under `<id>/`. An optional `server.public_base_url` derives each catalog
entry's `manifest_url`; station metadata can override it explicitly.
