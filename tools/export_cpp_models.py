#!/usr/bin/env python3
"""Export an official Meta EnCodec checkpoint for the Eigen runtime."""

import argparse
import struct
from pathlib import Path

import torch


MAGIC = b"ENCDMOD\0"
DECODER_ONLY_VERSION = 1
COMBINED_VERSION = 2
FLAG_CAUSAL = 1
FLAG_NORMALIZED = 2


def tensor(state, name):
    return state[name].detach().cpu().float().contiguous()


def convolution(state, prefix, transpose, normalized):
    weight_name = prefix + ".weight"
    if weight_name in state:
        weight = tensor(state, weight_name)
    else:
        weight = torch._weight_norm(
            tensor(state, prefix + ".weight_v"),
            tensor(state, prefix + ".weight_g"), dim=0)
    # Eigen consumes [out, kernel, in] for Conv1d and [in, kernel, out]
    # for ConvTranspose1d, while PyTorch stores the kernel last.
    yield weight.permute(0, 2, 1).contiguous().flatten()
    yield tensor(state, prefix + ".bias").flatten()
    if normalized:
        norm_prefix = prefix.rsplit(".", 1)[0] + ".norm"
        yield tensor(state, norm_prefix + ".weight").flatten()
        yield tensor(state, norm_prefix + ".bias").flatten()


def decoder_tensors(state, normalized):
    yield from convolution(state, "decoder.model.0.conv.conv", False, normalized)
    for layer in range(2):
        prefix = f"decoder.model.1.lstm."
        for item in ("weight_ih", "weight_hh", "bias_ih", "bias_hh"):
            yield tensor(state, f"{prefix}{item}_l{layer}").flatten()

    for transposed, residual in ((3, 4), (6, 7), (9, 10), (12, 13)):
        yield from convolution(state, f"decoder.model.{transposed}.convtr.convtr", True, normalized)
        yield from convolution(state, f"decoder.model.{residual}.block.1.conv.conv", False, normalized)
        yield from convolution(state, f"decoder.model.{residual}.block.3.conv.conv", False, normalized)
        yield from convolution(state, f"decoder.model.{residual}.shortcut.conv.conv", False, normalized)
    yield from convolution(state, "decoder.model.15.conv.conv", False, normalized)


def encoder_tensors(state, normalized):
    yield from convolution(state, "encoder.model.0.conv.conv", False, normalized)
    for residual, downsample in ((1, 3), (4, 6), (7, 9), (10, 12)):
        yield from convolution(state, f"encoder.model.{residual}.block.1.conv.conv", False, normalized)
        yield from convolution(state, f"encoder.model.{residual}.block.3.conv.conv", False, normalized)
        yield from convolution(state, f"encoder.model.{residual}.shortcut.conv.conv", False, normalized)
        yield from convolution(state, f"encoder.model.{downsample}.conv.conv", False, normalized)

    for layer in range(2):
        prefix = "encoder.model.13.lstm."
        for item in ("weight_ih", "weight_hh", "bias_ih", "bias_hh"):
            yield tensor(state, f"{prefix}{item}_l{layer}").flatten()
    yield from convolution(state, "encoder.model.15.conv.conv", False, normalized)


CHECKPOINT_URLS = {
    24000: "https://dl.fbaipublicfiles.com/encodec/v0/encodec_24khz-d7cc33bc.th",
    48000: "https://dl.fbaipublicfiles.com/encodec/v0/encodec_48khz-7e698e3e.th",
}


def export(checkpoint, output, sample_rate, include_encoder):
    state = (torch.load(checkpoint, map_location="cpu", weights_only=True) if checkpoint else
             torch.hub.load_state_dict_from_url(
                 CHECKPOINT_URLS[sample_rate], map_location="cpu", progress=True,
                 check_hash=True, weights_only=True))
    if sample_rate == 24000:
        channels, max_quantizers, flags = 1, 32, FLAG_CAUSAL
    else:
        channels, max_quantizers, flags = 2, 16, FLAG_NORMALIZED

    normalized = bool(flags & FLAG_NORMALIZED)
    encoder = (torch.cat(list(encoder_tensors(state, normalized)))
               if include_encoder else torch.empty(0, dtype=torch.float32))
    decoder = torch.cat(list(decoder_tensors(state, normalized)))
    rvq = torch.cat([
        tensor(state, f"quantizer.vq.layers.{level}._codebook.embed").flatten()
        for level in range(max_quantizers)
    ])
    if include_encoder:
        header = struct.pack("<8s8I", MAGIC, COMBINED_VERSION, sample_rate, channels,
                             max_quantizers, flags, encoder.numel(), decoder.numel(),
                             rvq.numel())
    else:
        header = struct.pack("<8s7I", MAGIC, DECODER_ONLY_VERSION, sample_rate,
                             channels, max_quantizers, flags, decoder.numel(),
                             rvq.numel())
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream:
        stream.write(header)
        if include_encoder:
            stream.write(encoder.numpy().tobytes())
        stream.write(decoder.numpy().tobytes())
        stream.write(rvq.numpy().tobytes())
    print(f"Wrote {output} ({output.stat().st_size / 1024 / 1024:.1f} MiB)")
    print(f"  {sample_rate} Hz, {channels} channel(s), {max_quantizers} codebooks")
    print(f"  encoder={encoder.numel()} floats, decoder={decoder.numel()} floats, "
          f"rvq={rvq.numel()} floats")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--checkpoint", type=Path,
        help="optional local Meta checkpoint; otherwise download the official model")
    parser.add_argument("--sample-rate", type=int, choices=(24000, 48000), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-encoder", action="store_true",
                        help="write a version-2 model containing encoder and decoder weights")
    args = parser.parse_args()
    export(args.checkpoint, args.output, args.sample_rate, args.include_encoder)


if __name__ == "__main__":
    main()
