#!/usr/bin/env python3
"""Local ELTCP timing harness; uses synthetic ECDC, no encoder or live input."""
from __future__ import annotations

import argparse
import json
import logging
import math
from pathlib import Path
import socket
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from encodec_live_streamer.config import Config
from encodec_live_streamer.ecdc import make_test_ecdc
from encodec_live_streamer.manifest import ManifestStore, crc32c
from encodec_live_streamer.tcp import TcpServer, encode_varint, read_varint


def receive_exact(connection, count):
    result = bytearray()
    while len(result) < count:
        block = connection.recv(count - len(result))
        if not block:
            raise EOFError("connection closed within response")
        result.extend(block)
    return bytes(result)


def summary(values):
    return {"n": len(values), "median_ms": statistics.median(values),
            "p95_ms": sorted(values)[math.ceil(len(values) * .95) - 1]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=200)
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--wait", action="store_true", help="Also measure a publication wait with heartbeats")
    args = parser.parse_args()
    if args.sessions < 1:
        parser.error("--sessions must be positive")
    records = []

    class Collector(logging.Handler):
        def emit(self, record):
            if record.msg == "ELTCP timing %s":
                records.append(json.loads(record.args[0]))

    logger = logging.getLogger("encodec_live_streamer.tcp")
    logger.handlers = [Collector()]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    with tempfile.TemporaryDirectory() as directory:
        config = Config(input="unused", output_dir=Path(directory), fsync=False,
                        tcp_port=0, tcp_enabled=True, tcp_diagnostics=args.diagnostics,
                        tcp_segment_cache_bytes=0 if args.no_cache else 8388608,
                        segment_duration=4.96, window_segments=12,
                        tcp_manifest_wait=.5, tcp_heartbeat_enabled=True,
                        tcp_heartbeat_interval=.02)
        store = ManifestStore(config)
        payload = make_test_ecdc(config.samples_per_segment, config.codebooks, config.model)
        payload += bytes(max(0, 990 - len(payload)))
        checksum = crc32c(payload)
        store.next_sequence = 693549

        def publish(index):
            store.publish_segment(payload, sample_count=config.samples_per_segment,
                                  pts_samples=index * config.samples_per_segment,
                                  program_date_time="2026-01-01T00:00:00Z", epoch="bench",
                                  discontinuity=index == 0, epoch_start_unix_ms=1767225600000)

        for index in range(12):
            publish(index)
        manifest_bytes = len(store.snapshot().manifest)
        server = TcpServer(config, store)
        server.start()
        measurements = {"i": [], "f": []}
        try:
            for _ in range(args.sessions):
                with socket.create_connection(server.address, timeout=2) as connection:
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    started = time.monotonic_ns()
                    connection.sendall(b"i")
                    assert receive_exact(connection, 1) == b"m"
                    receive_exact(connection, read_varint(connection))
                    measurements["i"].append((time.monotonic_ns() - started) / 1e6)
                    started = time.monotonic_ns()
                    connection.sendall(b"f" + encode_varint(693549))
                    assert receive_exact(connection, 1) == b"S"
                    length = read_varint(connection)
                    metadata = receive_exact(connection, 5)
                    assert int.from_bytes(metadata[:4], "big") == checksum
                    assert receive_exact(connection, length) == payload
                    measurements["f"].append((time.monotonic_ns() - started) / 1e6)
            if args.wait:
                with socket.create_connection(server.address, timeout=2) as connection:
                    connection.sendall(b"i")
                    assert receive_exact(connection, 1) == b"m"
                    receive_exact(connection, read_varint(connection))
                    timer = threading.Timer(.08, publish, args=(12,))
                    connection.sendall(b"f" + encode_varint(693561))
                    timer.start()
                    response = receive_exact(connection, 1)
                    while response == b"h":
                        response = receive_exact(connection, 1)
                    assert response == b"S"
                    receive_exact(connection, read_varint(connection) + 5)
                    timer.join()
        finally:
            server.close()
        result = dict(manifest_bytes=manifest_bytes, segment_bytes=len(payload),
                      platform=sys.platform, python=sys.version.split()[0],
                      loopback_round_trip={key: summary(values) for key, values in measurements.items()})
        fields = ("accept_to_command_ms", "snapshot_lock_wait_ms", "snapshot_lookup_ms",
                  "segment_lookup_ms", "file_read_ms", "frame_preparation_ms",
                  "publication_wait_ms", "write_ms", "processing_ms", "command_to_write_complete_ms")
        result["server_stages"] = {}
        for command in ("i", "f"):
            selected = [record for record in records if record.get("command") == command
                        and record.get("response") in ("m", "S") and record["publication_wait_ms"] == 0]
            if selected:
                result["server_stages"][command] = {
                    field: summary([record[field] for record in selected]) for field in fields}
        result["publication_wait_responses"] = [record for record in records
            if record.get("command") == "f" and record.get("response") == "S" and record["publication_wait_ms"] > 0]
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
