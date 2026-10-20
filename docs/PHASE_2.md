# Later phases

The MVP intentionally keeps FFmpeg and EnCodec in separate processes. That
keeps upgrades, failures, model loading, and the published bytes easy to inspect.

## Optional continuous HTTP transport

An Icecast-like mount could stream length-prefixed, independently decodable ECDC
objects over one chunked HTTP response. It should retain the same sequence,
epoch, timing, and discontinuity metadata as protocol v1. This is useful only
after measuring manifest polling overhead: it needs a custom origin service and
custom Android framing/reconnect code, and official ECDC alone cannot be endless
because its header declares total audio length.

## FFmpeg-native integration

Only after the service protocol and client behavior stabilize, consider:

- promote the included C++ EnCodec core from its current private model format
  and worker API to a versioned public library/runtime ABI;
- an FFmpeg encoder wrapper (`AVCodec`) for finite ECDC objects, or a filter that
  emits code tensors/packets;
- a muxer for this segmented live protocol, if FFmpeg's segmenting abstractions
  can preserve atomic manifest and cleanup behavior;
- timestamp propagation from `AVFrame.pts`, eliminating the MVP's wall-clock
  estimate;
- hardware/runtime benchmarking and backpressure policy.

That phase must preserve independently decodable chunks and non-LM output. It
should be justified by latency, resource, or operational measurements rather
than by integration aesthetics.
