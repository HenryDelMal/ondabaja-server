#pragma once

#include <cstdint>
#include <span>
#include <memory>
#include <filesystem>

namespace encodec
{

    struct model_info
    {
        unsigned int sample_rate{};
        unsigned int channels{};
        unsigned int max_quantizers{};
        bool causal{};
        bool normalized{};
        bool ecng{};
    };

    struct encoded_frame
    {
        std::span<const uint8_t> packet;
        std::size_t code_frames{};
        float scale{1.0f};
    };

//----------------------------------------------------------------------------------------------------------------

    unsigned int get_encodec_bps(unsigned int num_quantizers);
    unsigned int get_encoded_nquantizers(unsigned int bps);
    void set_num_threads(unsigned int threads);
    unsigned int get_num_threads();

//----------------------------------------------------------------------------------------------------------------

    class encoder
    {
    private:
        struct impl;
        std::unique_ptr<impl> state;
        
    public:
        encoder();
        explicit encoder(const std::filesystem::path& model_path);
        ~encoder();
        encoder(encoder&& other);
        encoder& operator=(encoder&& other);

        std::span<const uint8_t> encode(std::span<const float> audio, unsigned int num_quantizers);
        std::span<const uint8_t> encode(std::span<const float> audio, unsigned int num_quantizers,
                                        std::size_t beam_size);
        encoded_frame encode_frame(std::span<const float> audio, unsigned int num_quantizers);
        encoded_frame encode_frame(std::span<const float> audio, unsigned int num_quantizers,
                                   std::size_t beam_size);
        model_info info() const;
    };

//----------------------------------------------------------------------------------------------------------------

    class decoder
    {
    private:
        struct impl;
        std::unique_ptr<impl> state;
        
    public:
        decoder();
        explicit decoder(const std::filesystem::path& model_path);
        ~decoder();
        decoder(decoder&& other);
        decoder& operator=(decoder&& other);

        std::span<const float> decode(std::span<const uint8_t> packet, unsigned int num_quantizers);
        std::span<const float> decode(std::span<const uint8_t> packet, unsigned int num_quantizers,
                                      std::size_t code_frames);
        model_info info() const;
    };

//----------------------------------------------------------------------------------------------------------------

}
