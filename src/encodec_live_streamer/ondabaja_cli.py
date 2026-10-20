from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys

from .encoder import native_command, native_encoder_path
from .ffmpeg import command as ffmpeg_command
from .ondabaja import OndaBajaService, load_ondabaja_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ondabaja-server")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "check"):
        command = commands.add_parser(name)
        command.add_argument("--config", default="server.toml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config, streams = load_ondabaja_config(args.config)
        if args.command == "check":
            for station, stream in zip(config.stations, streams):
                if not stream.output_dir.is_absolute():
                    raise ValueError(f"station {station.id}: output_dir must be absolute")
                if shutil.which(stream.ffmpeg) is None:
                    raise RuntimeError(f"station {station.id}: FFmpeg not found: {stream.ffmpeg}")
                native = native_encoder_path(stream)
                if shutil.which(native) is None:
                    raise RuntimeError(f"station {station.id}: native encoder not found: {native}")
                if not stream.model_path.is_file():
                    raise RuntimeError(f"station {station.id}: combined model missing: {stream.model_path}")
                subprocess.run(native_command(stream, check_model=True), check=True,
                               capture_output=True, text=True)
                print(f"{station.id}: {station.title} -> {stream.output_dir} ({stream.model}, "
                      f"{stream.bandwidth_kbps:g} kbps, {stream.threads} encoder thread(s))")
                print("  FFmpeg:", " ".join(ffmpeg_command(stream)))
            print(f"catalog revision: {config.revision}; TCP: {config.host}:{config.port}; "
                  f"concurrent publisher limit: {config.max_streams}")
            print("configuration: OK")
            return 0
        OndaBajaService(config, streams, args.config).run()
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        logging.getLogger(__name__).error("%s", error)
        return 2


if __name__ == "__main__":
    sys.exit(main())
