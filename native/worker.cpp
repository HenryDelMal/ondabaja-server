#include <algorithm>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include "encodec.h"

namespace {

constexpr std::uint32_t MAX_MESSAGE_BYTES = 256u * 1024u * 1024u;
constexpr std::size_t HQ_FRAME_SAMPLES = 48'000;
constexpr std::size_t HQ_FRAME_STRIDE = 47'520;

struct options {
    std::string model_path;
    std::string codec{"EnCodec"};
    unsigned samplerate_khz{};
    unsigned codebooks{};
    unsigned threads{1};
    unsigned beam_size{8};
    bool check_model{};
};

unsigned parse_unsigned(std::string_view value, std::string_view name) {
    std::size_t consumed{};
    const auto parsed = std::stoul(std::string(value), &consumed);
    if (consumed != value.size() || parsed > std::numeric_limits<unsigned>::max())
        throw std::runtime_error("Invalid " + std::string(name));
    return static_cast<unsigned>(parsed);
}

options parse_options(int argc, char** argv) {
    options result;
    for (int index = 1; index < argc; ++index) {
        const std::string_view argument{argv[index]};
        if (argument == "--check-model") {
            result.check_model = true;
            continue;
        }
        if (index + 1 >= argc) throw std::runtime_error("Missing value for " + std::string(argument));
        const std::string_view value{argv[++index]};
        if (argument == "--codec") result.codec = value;
        else if (argument == "--model") result.model_path = value;
        else if (argument == "--samplerate") result.samplerate_khz = parse_unsigned(value, argument);
        else if (argument == "--codebooks") result.codebooks = parse_unsigned(value, argument);
        else if (argument == "--threads") result.threads = parse_unsigned(value, argument);
        else if (argument == "--beam-size") result.beam_size = parse_unsigned(value, argument);
        else throw std::runtime_error("Unknown argument: " + std::string(argument));
    }
    if (result.model_path.empty()) throw std::runtime_error("--model is required");
    if (result.codec != "EnCodec" && result.codec != "ECNG")
        throw std::runtime_error("--codec must be EnCodec or ECNG");
    if (result.samplerate_khz != 24 && result.samplerate_khz != 48)
        throw std::runtime_error("--samplerate must be 24 or 48");
    if (result.codebooks == 0) throw std::runtime_error("--codebooks must be positive");
    if (result.threads == 0) throw std::runtime_error("--threads must be positive");
    if (result.beam_size == 0 || result.beam_size > 64)
        throw std::runtime_error("--beam-size must be between 1 and 64");
    return result;
}

void read_exact(char* destination, std::size_t size) {
    std::cin.read(destination, static_cast<std::streamsize>(size));
    if (std::cin.gcount() != static_cast<std::streamsize>(size))
        throw std::runtime_error("Truncated request from publisher");
}

void append_u32_be(std::vector<std::uint8_t>& output, std::uint32_t value) {
    output.push_back(static_cast<std::uint8_t>(value >> 24));
    output.push_back(static_cast<std::uint8_t>(value >> 16));
    output.push_back(static_cast<std::uint8_t>(value >> 8));
    output.push_back(static_cast<std::uint8_t>(value));
}

void append_float_be(std::vector<std::uint8_t>& output, float value) {
    static_assert(sizeof(value) == sizeof(std::uint32_t));
    std::uint32_t bits;
    std::memcpy(&bits, &value, sizeof(bits));
    append_u32_be(output, bits);
}

void append_bytes(std::vector<std::uint8_t>& output, std::span<const std::uint8_t> input) {
    output.insert(output.end(), input.begin(), input.end());
}

std::vector<std::uint8_t> ecdc_header(const encodec::model_info& info,
                                      std::size_t sample_frames, unsigned codebooks,
                                      std::string_view codec) {
    const std::string model = codec == "ECNG" ? "ecng_24khz" :
        (info.sample_rate == 24'000 ? "encodec_24khz" : "encodec_48khz");
    const std::string metadata = "{\"m\":\"" + model + "\",\"al\":" +
        std::to_string(sample_frames) + ",\"nc\":" + std::to_string(codebooks) +
        ",\"lm\":false}";
    std::vector<std::uint8_t> output;
    output.reserve(9 + metadata.size());
    output.insert(output.end(), {'E', 'C', 'D', 'C', 0});
    append_u32_be(output, static_cast<std::uint32_t>(metadata.size()));
    output.insert(output.end(), metadata.begin(), metadata.end());
    return output;
}

std::vector<std::uint8_t> encode_segment(encodec::encoder& encoder,
                                         const encodec::model_info& info,
                                         std::span<const float> audio,
                                         unsigned codebooks,
                                         unsigned beam_size,
                                         std::string_view codec) {
    const std::size_t sample_frames = audio.size() / info.channels;
    auto output = ecdc_header(info, sample_frames, codebooks, codec);
    if (info.sample_rate == 24'000) {
        const auto frame = encoder.encode_frame(audio, codebooks, beam_size);
        append_bytes(output, frame.packet);
        return output;
    }

    for (std::size_t offset = 0; offset < sample_frames; offset += HQ_FRAME_STRIDE) {
        const std::size_t length = std::min(HQ_FRAME_SAMPLES, sample_frames - offset);
        const auto begin = audio.data() + offset * info.channels;
        const auto frame = encoder.encode_frame(
            std::span<const float>{begin, length * info.channels}, codebooks, beam_size);
        append_float_be(output, frame.scale);
        append_bytes(output, frame.packet);
    }
    return output;
}

void write_response(std::span<const std::uint8_t> payload) {
    if (payload.empty() || payload.size() > MAX_MESSAGE_BYTES)
        throw std::runtime_error("Encoded segment exceeds worker protocol limit");
    const auto size = static_cast<std::uint32_t>(payload.size());
    std::cout.write(reinterpret_cast<const char*>(&size), sizeof(size));
    std::cout.write(reinterpret_cast<const char*>(payload.data()),
                    static_cast<std::streamsize>(payload.size()));
    std::cout.flush();
    if (!std::cout) throw std::runtime_error("Publisher closed the worker output pipe");
}

} // namespace

int main(int argc, char** argv) {
    static_assert(std::endian::native == std::endian::little,
                  "The PCM worker protocol currently requires a little-endian host");
    try {
        const auto arguments = parse_options(argc, argv);
        encodec::set_num_threads(arguments.threads);
        encodec::encoder encoder(arguments.model_path);
        const auto info = encoder.info();
        if (info.ecng != (arguments.codec == "ECNG"))
            throw std::runtime_error("Model identity does not match --codec");
        if (info.sample_rate != arguments.samplerate_khz * 1'000u)
            throw std::runtime_error("Model sample rate does not match --samplerate");
        if (arguments.codebooks > info.max_quantizers)
            throw std::runtime_error("Requested codebooks exceed model capacity");
        if ((info.sample_rate == 24'000 && info.channels != 1) ||
            (info.sample_rate == 48'000 && info.channels != 2))
            throw std::runtime_error("Model channel layout is incompatible");
        if ((arguments.codec == "EnCodec" && info.sample_rate == 24'000 &&
             (!info.causal || info.normalized || info.max_quantizers != 32)) ||
            (arguments.codec == "ECNG" &&
             (info.sample_rate != 24'000 || info.channels != 1 || !info.causal ||
              info.normalized || info.max_quantizers != 4)) ||
            (info.sample_rate == 48'000 &&
             (arguments.codec != "EnCodec" || info.causal || !info.normalized || info.max_quantizers != 16)))
            throw std::runtime_error("Model architecture flags are incompatible");
        if (arguments.check_model) {
            std::cout << "native model: " << info.sample_rate << " Hz, " << info.channels
                      << " channel(s), " << arguments.codebooks << " codebooks, threads="
                      << encodec::get_num_threads() << ", beam_size="
                      << arguments.beam_size << '\n';
            return 0;
        }

        while (true) {
            std::uint32_t byte_count{};
            std::cin.read(reinterpret_cast<char*>(&byte_count), sizeof(byte_count));
            if (std::cin.eof() && std::cin.gcount() == 0) return 0;
            if (std::cin.gcount() != sizeof(byte_count))
                throw std::runtime_error("Truncated request length from publisher");
            if (byte_count == 0) return 0;
            if (byte_count > MAX_MESSAGE_BYTES || byte_count % (info.channels * sizeof(float)) != 0)
                throw std::runtime_error("Invalid PCM request size");
            std::vector<float> audio(byte_count / sizeof(float));
            read_exact(reinterpret_cast<char*>(audio.data()), byte_count);
            write_response(encode_segment(
                encoder, info, audio, arguments.codebooks, arguments.beam_size,
                arguments.codec));
        }
    } catch (const std::exception& error) {
        std::cerr << "encodec-live-native: " << error.what() << '\n';
        return 2;
    }
}
