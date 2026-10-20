#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <vector>
#include <Eigen/Dense>
#include "encodec.h"

//----------------------------------------------------------------------------------------------------------------

using MatrixXf      = Eigen::Matrix<float, -1, -1, Eigen::RowMajor>;
using MatrixXu16    = Eigen::Matrix<uint16_t, -1, -1, Eigen::RowMajor>;
using VectorXf      = Eigen::Vector<float, -1>;
using ArrayXf       = Eigen::Array<float, -1, 1>;

//----------------------------------------------------------------------------------------------------------------

extern const float       ENCODER_WEIGHTS[];
extern const std::size_t ENCODER_SIZE;
extern const float       DECODER_WEIGHTS[];
extern const std::size_t DECODER_SIZE;
extern const float       RVQ_WEIGHTS[];
extern const std::size_t RVQ_SIZE;

//----------------------------------------------------------------------------------------------------------------

namespace encodec
{

    struct cpu_thread_state
    {
        cpu_thread_state() { Eigen::setNbThreads(1); }
    };

    cpu_thread_state& cpu_threads()
    {
        static cpu_thread_state state;
        return state;
    }

    void set_num_threads(unsigned int threads)
    {
        if (threads == 0) throw std::runtime_error("Thread count must be positive");
#if !defined(ENCODEC_HAS_OPENMP)
        if (threads > 1)
            throw std::runtime_error("This build does not include OpenMP threading support");
#endif
        (void)cpu_threads();
        Eigen::setNbThreads(int(threads));
    }

    unsigned int get_num_threads()
    {
        (void)cpu_threads();
        return unsigned(Eigen::nbThreads());
    }

//----------------------------------------------------------------------------------------------------------------
//----------------------------------------------------------------------------------------------------------------
// MATH
//----------------------------------------------------------------------------------------------------------------
//----------------------------------------------------------------------------------------------------------------

    constexpr void add(std::span<const float> a, std::span<const float> b, std::span<float> c)
    {
        for (size_t i{0} ; i < a.size() ; ++i)
            c[i] = a[i] + b[i];
    }

//----------------------------------------------------------------------------------------------------------------
//----------------------------------------------------------------------------------------------------------------
// CONSTANTS
//----------------------------------------------------------------------------------------------------------------
//----------------------------------------------------------------------------------------------------------------
    
    constexpr double    SAMPLE_RATE     = 24000;
    constexpr unsigned  STRIDE          = 320;
    constexpr unsigned  NLEVELS         = 32;
    constexpr unsigned  CODEBOOK_SIZE   = 1024;
    constexpr unsigned  CODEBOOK_DIM    = 128;

    // RVQ encoder search width.
    //
    // 1  = original EnCodec greedy RVQ encoder (bit-identical path)
    // >1 = beam-search RVQ encoder
    //
    // Suggested starting points: 4, 8, 16.
    constexpr size_t RVQ_BEAM_SIZE = 1;

    constexpr uint32_t MODEL_CAUSAL = 1u << 0;
    constexpr uint32_t MODEL_NORMALIZED = 1u << 1;
    constexpr uint32_t MODEL_ECNG = 1u << 2;

    struct runtime_model
    {
        model_info info;
        std::vector<float> encoder_weights;
        std::vector<float> decoder_weights;
        std::vector<float> rvq_weights;
    };

    runtime_model load_runtime_model(const std::filesystem::path& path)
    {
        std::ifstream input(path, std::ios::binary);
        if (!input) throw std::runtime_error("Cannot open EnCodec model: " + path.string());

        char magic[8]{};
        uint32_t version{};
        input.read(magic, sizeof(magic));
        input.read(reinterpret_cast<char*>(&version), sizeof(version));
        if (!input || std::memcmp(magic, "ENCDMOD", 7) != 0 || (version != 1 && version != 2))
            throw std::runtime_error("Unsupported EnCodec model file");

        uint32_t sample_rate{}, channels{}, max_quantizers{}, flags{};
        uint32_t encoder_floats{}, decoder_floats{}, rvq_floats{};
        input.read(reinterpret_cast<char*>(&sample_rate), sizeof(sample_rate));
        input.read(reinterpret_cast<char*>(&channels), sizeof(channels));
        input.read(reinterpret_cast<char*>(&max_quantizers), sizeof(max_quantizers));
        input.read(reinterpret_cast<char*>(&flags), sizeof(flags));
        if (version == 2)
            input.read(reinterpret_cast<char*>(&encoder_floats), sizeof(encoder_floats));
        input.read(reinterpret_cast<char*>(&decoder_floats), sizeof(decoder_floats));
        input.read(reinterpret_cast<char*>(&rvq_floats), sizeof(rvq_floats));
        constexpr uint32_t MAX_MODEL_FLOATS = 256u * 1024u * 1024u;
        const bool ecng = (flags & MODEL_ECNG) != 0;
        if (!input || (sample_rate != 24000 && sample_rate != 48000) ||
            (channels != 1 && channels != 2) || max_quantizers == 0 ||
            encoder_floats > MAX_MODEL_FLOATS || decoder_floats > MAX_MODEL_FLOATS ||
            rvq_floats > MAX_MODEL_FLOATS ||
            (ecng && (sample_rate != 24000 || channels != 1 || max_quantizers != 4 ||
                      (flags & MODEL_CAUSAL) == 0 || (flags & MODEL_NORMALIZED) != 0 ||
                      decoder_floats != 0)))
            throw std::runtime_error("Invalid EnCodec model configuration");

        runtime_model model;
        model.info = {sample_rate, channels, max_quantizers,
                      (flags & MODEL_CAUSAL) != 0, (flags & MODEL_NORMALIZED) != 0, ecng};
        model.encoder_weights.resize(encoder_floats);
        model.decoder_weights.resize(decoder_floats);
        model.rvq_weights.resize(rvq_floats);
        input.read(reinterpret_cast<char*>(model.encoder_weights.data()),
                   std::streamsize(model.encoder_weights.size() * sizeof(float)));
        input.read(reinterpret_cast<char*>(model.decoder_weights.data()),
                   std::streamsize(model.decoder_weights.size() * sizeof(float)));
        input.read(reinterpret_cast<char*>(model.rvq_weights.data()),
                   std::streamsize(model.rvq_weights.size() * sizeof(float)));
        if (!input) throw std::runtime_error("Truncated EnCodec model file");
        return model;
    }

//----------------------------------------------------------------------------------------------------------------
//----------------------------------------------------------------------------------------------------------------
// RVQ
//----------------------------------------------------------------------------------------------------------------
//----------------------------------------------------------------------------------------------------------------
    
    constexpr void pack_codes(std::span<const uint16_t> codes, std::span<uint8_t> bytes)
    {
        const size_t nblocks = codes.size()/4; // 40 bits blocks
        const size_t rem     = codes.size()%4;

        for (size_t b{0} ; b < nblocks ; ++b)
        {
            const uint64_t word = uint64_t(codes[b*4+0]) |
                                 (uint64_t(codes[b*4+1]) << 10) | 
                                 (uint64_t(codes[b*4+2]) << 20) |
                                 (uint64_t(codes[b*4+3]) << 30);
            bytes[b*5+0] =  word        & 0xffull;
            bytes[b*5+1] = (word >>  8) & 0xffull;
            bytes[b*5+2] = (word >> 16) & 0xffull;
            bytes[b*5+3] = (word >> 24) & 0xffull;
            bytes[b*5+4] = (word >> 32) & 0xffull;
        }
        
        if (rem > 0)
        {
            uint64_t word{0};
            for (size_t k{0} ; k < rem ; ++k)
                word |= uint64_t(codes[nblocks*4+k]) << (10 * k);
            
            const size_t tail_bytes = (rem * 10 + 7) / 8;
            for (size_t j{0}; j < tail_bytes; ++j)
                bytes[nblocks*5+j] = uint8_t(word >> (8*j));
        }
    }

    constexpr void unpack_bits(std::span<const uint8_t> bytes, std::span<uint16_t> codes)
    {
        const size_t nblocks = bytes.size()/5; // 40 bits blocks
        const size_t rem     = bytes.size()%5;

        for (size_t b{0} ; b < nblocks ; ++b)
        {
            const uint64_t word = uint64_t(bytes[b*5+0])        |
                                 (uint64_t(bytes[b*5+1]) << 8)  |
                                 (uint64_t(bytes[b*5+2]) << 16) |
                                 (uint64_t(bytes[b*5+3]) << 24) |
                                 (uint64_t(bytes[b*5+4]) << 32);
            codes[b*4+0] =  word        & 0x3ffull;
            codes[b*4+1] = (word >> 10) & 0x3ffull;
            codes[b*4+2] = (word >> 20) & 0x3ffull;
            codes[b*4+3] = (word >> 30) & 0x3ffull;
        }

        if (rem > 0)
        {
            uint64_t word{0};
            for (size_t j{0} ; j < rem ; ++j)
                word |= uint64_t(bytes[nblocks*5+j]) << (j*8);

            const size_t tail_codes = (rem*8) / 10;
            for (size_t k{0}; k < tail_codes; ++k)
                codes[nblocks*4+k] = (word >> (10 * k)) & 0x3ffull;
        }
    }

//----------------------------------------------------------------------------------------------------------------

    unsigned int get_encodec_bps(unsigned int nlevels)      { return (SAMPLE_RATE / STRIDE) * nlevels * 10; }
    unsigned int get_encoded_nquantizers(unsigned int bps)  { return (bps / 10) * STRIDE / SAMPLE_RATE; }

//----------------------------------------------------------------------------------------------------------------

    struct rvq
    {
        std::span<const float> weights;
        size_t max_levels;
        std::vector<VectorXf> Cnorms;
        MatrixXf dists;
        std::vector<uint16_t> codes;
        std::vector<uint8_t>  codes_packed;
        std::vector<float>    feats;

        rvq(std::span<const float> weights_ = {RVQ_WEIGHTS, RVQ_SIZE}, size_t max_levels_ = NLEVELS)
        : weights{weights_}, max_levels{max_levels_}, Cnorms(max_levels)
        {
            if (weights.size() != max_levels * CODEBOOK_SIZE * CODEBOOK_DIM)
                throw std::runtime_error("Invalid RVQ weight count");
            for (size_t l{0} ; l < max_levels ; ++l)
                Cnorms[l] = codebook(l).rowwise().squaredNorm();
        }

        Eigen::Map<const MatrixXf> codebook(size_t l) const
        {
            return Eigen::Map<const MatrixXf>(weights.data() + l*CODEBOOK_SIZE*CODEBOOK_DIM,
                                               CODEBOOK_SIZE, CODEBOOK_DIM);
        }

        std::span<const uint8_t> encode(std::span<float> feats, size_t nlevels,
                                        size_t beam_size = RVQ_BEAM_SIZE)
        {
            // RVQ Encode
            const size_t T = feats.size() / CODEBOOK_DIM;
            codes.resize(T*nlevels);
            codes_packed.resize((codes.size()*10 + 7) / 8);

            auto X = Eigen::Map<MatrixXf>(&feats[0], T, CODEBOOK_DIM);

            if (beam_size == 0)
                throw std::runtime_error("RVQ beam size must be at least 1");

            // Keep the original greedy implementation intact for beam_size == 1.
            // Besides being faster, this provides an exact compatibility/reference
            // path against the pre-beam-search encoder.
            if (beam_size == 1)
            {
                for (size_t l{0} ; l < nlevels ; ++l)
                {
                    auto C = codebook(l);
                    dists.noalias() = -2.0f * X * C.transpose();
                    dists.rowwise() += Cnorms[l].transpose();

                    for (size_t t{0}; t < T; ++t)
                    {
                        Eigen::Index best_idx{0};
                        dists.row(t).minCoeff(&best_idx);
                        X.row(t) -= C.row(best_idx);
                        codes[t*nlevels+l] = static_cast<uint16_t>(best_idx);
                    }
                }

                pack_codes(codes, codes_packed);
                return codes_packed;
            }

            // Beam search is performed independently for each latent frame.
            //
            // A beam contains:
            //   - the residual after the codewords selected so far
            //   - the sequence of selected codebook indices
            //
            // At every RVQ level, each active beam is expanded with all 1024
            // codewords from that level. Only the best `beam_size` resulting
            // residuals are retained.
            //
            // The score is the CURRENT squared residual norm:
            //
            //   ||x - sum(q_i)||^2
            //
            // We intentionally do not accumulate intermediate errors because
            // the final residual is the objective that greedy RVQ minimizes
            // approximately and beam search minimizes more globally.
            struct beam_candidate
            {
                float    error{};
                size_t   parent{};
                uint16_t code{};
            };

            MatrixXf residuals;
            MatrixXf next_residuals;
            MatrixXf beam_dists;

            std::vector<std::vector<uint16_t>> paths;
            std::vector<std::vector<uint16_t>> next_paths;
            std::vector<beam_candidate> candidates;

            for (size_t t{0}; t < T; ++t)
            {
                residuals.resize(1, CODEBOOK_DIM);
                residuals.row(0) = X.row(t);

                paths.clear();
                paths.resize(1);
                paths[0].reserve(nlevels);

                for (size_t l{0}; l < nlevels; ++l)
                {
                    auto C = codebook(l);
                    const size_t active_beams = static_cast<size_t>(residuals.rows());

                    // Squared Euclidean distance:
                    //
                    //   ||r-c||^2 = ||r||^2 - 2*r.c + ||c||^2
                    //
                    // The original greedy encoder omits ||r||^2 because there
                    // is only one residual per frame and it is constant across
                    // all codeword choices. With multiple parent beams, each
                    // residual has a different norm, so it MUST be included.
                    beam_dists.noalias() = -2.0f * residuals * C.transpose();
                    beam_dists.rowwise() += Cnorms[l].transpose();

                    const VectorXf residual_norms =
                        residuals.rowwise().squaredNorm();

                    beam_dists.colwise() += residual_norms;

                    candidates.clear();
                    candidates.reserve(active_beams * CODEBOOK_SIZE);

                    for (size_t b{0}; b < active_beams; ++b)
                    {
                        for (size_t c{0}; c < CODEBOOK_SIZE; ++c)
                        {
                            candidates.push_back({
                                beam_dists(static_cast<Eigen::Index>(b),
                                           static_cast<Eigen::Index>(c)),
                                b,
                                static_cast<uint16_t>(c)
                            });
                        }
                    }

                    const size_t keep = std::min(beam_size, candidates.size());

                    // Deterministic tie-breaking makes repeated encodes stable.
                    const auto better = [](const beam_candidate& a,
                                           const beam_candidate& b)
                    {
                        if (a.error < b.error) return true;
                        if (a.error > b.error) return false;
                        if (a.parent < b.parent) return true;
                        if (a.parent > b.parent) return false;
                        return a.code < b.code;
                    };

                    if (keep < candidates.size())
                    {
                        std::nth_element(candidates.begin(),
                                         candidates.begin() + keep,
                                         candidates.end(),
                                         better);
                        candidates.resize(keep);
                    }

                    std::sort(candidates.begin(), candidates.end(), better);

                    next_residuals.resize(static_cast<Eigen::Index>(keep),
                                          CODEBOOK_DIM);
                    next_paths.clear();
                    next_paths.resize(keep);

                    for (size_t b{0}; b < keep; ++b)
                    {
                        const auto& candidate = candidates[b];

                        next_residuals.row(static_cast<Eigen::Index>(b)) =
                            residuals.row(static_cast<Eigen::Index>(candidate.parent))
                            - C.row(candidate.code);

                        next_paths[b] = paths[candidate.parent];
                        next_paths[b].push_back(candidate.code);
                    }

                    residuals.swap(next_residuals);
                    paths.swap(next_paths);
                }

                // Candidates were sorted at every stage, therefore beam 0 has
                // the smallest final residual norm.
                const auto& best_path = paths.front();
                for (size_t l{0}; l < nlevels; ++l)
                    codes[t*nlevels+l] = best_path[l];

                // Preserve the original function's side effect: `feats`
                // contains the final RVQ residual after encoding.
                X.row(t) = residuals.row(0);
            }

            // Packet structure is unchanged: the selected indices are packed
            // exactly as before (10 bits per codebook index).
            pack_codes(codes, codes_packed);
            return codes_packed;
        }

        std::span<float> decode(std::span<const uint8_t> codes_packed, size_t nlevels, size_t code_frames = 0)
        {
            // Unpack bits
            const size_t available_codes = (codes_packed.size()*8)/10;
            const size_t T = code_frames ? code_frames : available_codes/nlevels;
            const size_t ncodes = T*nlevels;
            if (ncodes > available_codes) throw std::runtime_error("Packet is shorter than its code-frame count");
            codes.resize(ncodes);
            feats.resize(T*CODEBOOK_DIM);
            unpack_bits(codes_packed, codes);

            auto X = Eigen::Map<const MatrixXu16>(&codes[0], T, nlevels);
            auto Y = Eigen::Map<MatrixXf>(&feats[0], T, CODEBOOK_DIM);
            Y.setZero();

            // RVQ decode
            for (size_t l{0}; l < nlevels; ++l)
            {
                auto C = codebook(l);
                for (size_t t{0}; t < T; ++t)
                    Y.row(t) += C.row(X(t,l));
            }

            return feats;
        }
    };

//----------------------------------------------------------------------------------------------------------------
//----------------------------------------------------------------------------------------------------------------
// ACTIVATIONS
//----------------------------------------------------------------------------------------------------------------
//----------------------------------------------------------------------------------------------------------------

    template <class Derived>
    auto elu(const Eigen::ArrayBase<Derived>& x, float alpha = 1.0)
    {
        return (x > 0.0f).select(x, alpha * (x.exp() - 1.0f));
    }

    template <class Derived>
    auto sigmoid(const Eigen::ArrayBase<Derived>& x)
    {
        return (1.0f + (-x).exp()).inverse();
    }

//----------------------------------------------------------------------------------------------------------------

    struct elu_layer
    {
        bool                inplace{};
        float               alpha{};
        std::vector<float>  tmp;

        elu_layer(bool inplace_, float alpha_ = 1.0) : inplace{inplace_}, alpha{alpha_} {}

        std::span<float> operator()(std::span<float> input)
        {
            if (inplace)
            {
                auto x = Eigen::Map<ArrayXf>(input.data(), input.size());
                x = elu(x, alpha);
                return input;
            }
            else
            {
                tmp.resize(input.size());
                auto x = Eigen::Map<const ArrayXf>(input.data(), input.size());
                auto y = Eigen::Map<ArrayXf>(tmp.data(), tmp.size());
                y = elu(x, alpha);
                return tmp;
            }
        }
    };

//----------------------------------------------------------------------------------------------------------------

    struct group_norm
    {
        bool enabled{};
        VectorXf weight;
        VectorXf bias;

        group_norm(size_t channels, bool enabled_)
        : enabled{enabled_}, weight(channels), bias(channels) {}

        auto load_weights(std::span<const float> data) -> std::span<const float>
        {
            if (!enabled) return data;
            const size_t count = size_t(weight.size() + bias.size());
            if (data.size() < count) throw std::runtime_error("Not enough data in GroupNorm weights");
            weight = Eigen::Map<const VectorXf>(data.data(), weight.size());
            bias = Eigen::Map<const VectorXf>(data.data() + weight.size(), bias.size());
            return data.subspan(count);
        }

        void apply(std::span<float> values)
        {
            if (!enabled || values.empty()) return;
            const size_t channels = size_t(weight.size());
            auto x = Eigen::Map<MatrixXf>(values.data(), values.size()/channels, channels);
            const float mean = x.mean();
            const float variance = (x.array() - mean).square().mean();
            x.array() = (x.array() - mean) / std::sqrt(variance + 1.0e-5f);
            x.array().rowwise() *= weight.transpose().array();
            x.array().rowwise() += bias.transpose().array();
        }
    };

//----------------------------------------------------------------------------------------------------------------
//----------------------------------------------------------------------------------------------------------------
// NN
//----------------------------------------------------------------------------------------------------------------
//----------------------------------------------------------------------------------------------------------------

    struct linear
    {
        MatrixXf w;
        VectorXf b;
        MatrixXf out;
        group_norm norm;

        linear(size_t nin_, size_t nout_, bool normalized = false)
        : w(nout_, nin_),
          b(nout_),
          norm(nout_, normalized)
        {
        }

        auto load_weights(std::span<const float> data) -> std::span<const float>
        {
            if (data.size() < size_t(w.size() + b.size())) throw std::runtime_error("Not enough data in weights");
            size_t off{0};
            auto w_ = Eigen::Map<const MatrixXf>(data.subspan(off, w.size()).data(), nout(), nin()); off += w.size();
            auto b_ = Eigen::Map<const VectorXf>(data.subspan(off, b.size()).data(), nout());        off += b.size();
            w       = w_;
            b       = b_;
            return norm.load_weights(data.subspan(off));
        }

        size_t nin()  {return w.cols();}
        size_t nout() {return w.rows();}

        std::span<float> operator()(std::span<const float> input)
        {
            const size_t Tin = input.size() / nin();
            auto x = Eigen::Map<const MatrixXf>(input.data(), Tin, nin());
            out.noalias() = x * w.transpose() ;
            out.rowwise() += b.transpose();
            std::span<float> result{out.data(), static_cast<size_t>(out.size())};
            norm.apply(result);
            return result;
        }
    };

//----------------------------------------------------------------------------------------------------------------

    struct conv
    {
        size_t   nin{};
        size_t   nout{};
        size_t   k{};
        size_t   s{};
        size_t   pad() {return (k - 1) + 1 - s;}
        MatrixXf w;         // shape [nout,k*nin]
        VectorXf b;         // shape [nout]
        MatrixXf patches;   // shape [Tout, k*nin]
        MatrixXf out;       // [Tout_padded, nout]
        bool causal;
        group_norm norm;

        conv(size_t nin_, size_t nout_, size_t k_, size_t s_=1,
             bool causal_=true, bool normalized=false)
        : nin{nin_}, 
          nout{nout_}, 
          k{k_}, 
          s{s_}, 
          w(nout, k*nin),
          b(nout),
          causal{causal_},
          norm(nout_, normalized)
        {}

        auto load_weights(std::span<const float> data) -> std::span<const float>
        {
            if (data.size() < size_t(w.size() + b.size())) throw std::runtime_error("Not enough data in weights");
            size_t off{0};
            auto w_ = Eigen::Map<const MatrixXf>(data.subspan(off, w.size()).data(), nout, k*nin); off += w.size();
            auto b_ = Eigen::Map<const VectorXf>(data.subspan(off, b.size()).data(), nout);        off += b.size();
            w       = w_;
            b       = b_;
            return norm.load_weights(data.subspan(off));
        }

        std::span<float> operator()(std::span<const float> input)
        {
            const size_t p    = pad();
            const size_t Tin  = input.size() / nin;
            // Meta's SConv1d pads incomplete strided hops on the right so the
            // encoder always produces ceil(T / stride) frames. This matters
            // for the final, shorter ECDC segment.
            const size_t extra_right = (s - Tin % s) % s;
            const size_t left = causal ? p : p - p/2;
            const size_t right = p - left + extra_right;
            const size_t Tinp = Tin+left+right;
            const size_t Tout = (Tinp-k)/s + 1;

            patches.resize(Tout, k*nin);

            // im2col : patches[j, :] = padded_input[j*s : j*s+k, :]
            auto reflected = [Tin](long index) -> size_t
            {
                while (index < 0 || index >= long(Tin))
                    index = index < 0 ? -index : 2*long(Tin)-2-index;
                return size_t(index);
            };
            for (size_t i{0}; i < Tout; ++i)
                for (size_t kk{0}; kk < k; ++kk)
                {
                    const auto ti = reflected(long(i*s + kk) - long(left));
                    std::copy_n(input.data()+ti*nin, nin, patches.data()+(i*k+kk)*nin);
                }

            // GEMM
            out.noalias() = patches * w.transpose();
            out.rowwise() += b.transpose();
            std::span<float> result{out.data(), (size_t)out.size()};
            norm.apply(result);
            return result;
        }
    };

//----------------------------------------------------------------------------------------------------------------

    struct conv_transpose
    {
        size_t   nin{};
        size_t   nout{};
        size_t   k{};
        size_t   s{};
        size_t   pad() {return (k - 1) + 1 - s;}
        MatrixXf w;         // [nin, k*nout], raw layout [nin,k,nout]
        VectorXf b;         // [nout]
        MatrixXf patches;   // [Tin, k*nout]
        MatrixXf out;       // [Tout_padded, nout]
        bool causal;
        group_norm norm;

        conv_transpose(size_t nin_, size_t nout_, size_t k_, size_t s_ = 1,
                       bool causal_=true, bool normalized=false)
        : nin{nin_}, 
          nout{nout_}, 
          k{k_}, 
          s{s_}, 
          w(nin,k*nout),
          b(nout),
          causal{causal_},
          norm(nout_, normalized)
        {}

        auto load_weights(std::span<const float> data) -> std::span<const float>
        {
            if (data.size() < size_t(w.size() + b.size())) throw std::runtime_error("Not enough data in weights");
            size_t off{0};
            auto w_ = Eigen::Map<const MatrixXf>(data.subspan(off, w.size()).data(), nin, k*nout); off += w.size();
            auto b_ = Eigen::Map<const VectorXf>(data.subspan(off, b.size()).data(), nout);        off += b.size();
            w       = w_;
            b       = b_;
            return norm.load_weights(data.subspan(off));
        }

        std::span<float> operator()(std::span<const float> input)
        {
            const size_t Tin         = input.size() / nin;
            const size_t p           = pad();
            const size_t Tout_padded = (Tin - 1) * s + (k - 1) + 1;
            const size_t Tout        = Tout_padded - p;
            out.setZero(Tout_padded, nout);

            // GEMM
            auto X = Eigen::Map<const MatrixXf>(input.data(), Tin, nin);
            patches.noalias() = X * w; // [Tin, k*nout]

            // col2im / overlap-add
            for (size_t t{0}; t < Tin; ++t)
            {
                for (size_t kk{0}; kk < k; ++kk)
                {
                    const size_t to = t*s + kk;
                    float* destination = out.data() + to*nout;
                    const float* source = patches.data() + (t*k + kk)*nout;
                    for (size_t channel{0}; channel < nout; ++channel)
                        destination[channel] += source[channel];
                }
            }

            // Add bias
            out.rowwise() += b.transpose();
            std::span<float> padded{out.data(), size_t(out.size())};
            norm.apply(padded);
            const size_t left = causal ? 0 : p - p/2;
            return std::span<float>{out.data() + left*nout, Tout*nout};
        }
    };

//----------------------------------------------------------------------------------------------------------------

    struct lstm_cell
    {
        MatrixXf wih;
        MatrixXf whh;
        VectorXf bias;
        VectorXf gates;
        VectorXf h;
        VectorXf c;
        MatrixXf xw;      // [T, 4H]
        MatrixXf out;     // [T, H]

        lstm_cell(size_t input_size, size_t hidden_size)
        : wih(4*hidden_size, input_size),
          whh(4*hidden_size, hidden_size),
          bias(4*hidden_size),
          gates(4*hidden_size),
          h(hidden_size),
          c(hidden_size)
        {
        }

        auto load_weights(std::span<const float> data) -> std::span<const float>
        {
            const size_t total_weights = wih.size()+whh.size()+bias.size()*2;
            if (data.size() < total_weights) throw std::runtime_error("Not enough data in weights");
            size_t off{0};
            auto wih_   = Eigen::Map<const MatrixXf>(data.subspan(off, wih.size()).data(), wih.rows(), wih.cols()); off += wih.size();
            auto whh_   = Eigen::Map<const MatrixXf>(data.subspan(off, whh.size()).data(), whh.rows(), whh.cols()); off += whh.size();
            auto bih_   = Eigen::Map<const VectorXf>(data.subspan(off, bias.size()).data(), bias.size());           off += bias.size();
            auto bhh_   = Eigen::Map<const VectorXf>(data.subspan(off, bias.size()).data(), bias.size());           off += bias.size();
            wih         = wih_;
            whh         = whh_;
            bias        = bih_ + bhh_;
            return data.subspan(off);
        }

        void apply_gates()
        {
            const size_t H = h.size();
            auto i = gates.segment(0 * H, H).array();
            auto f = gates.segment(1 * H, H).array();
            auto g = gates.segment(2 * H, H).array();
            auto o = gates.segment(3 * H, H).array();
            c.array() = sigmoid(f) * c.array()  + sigmoid(i) * g.tanh();
            h.array() = sigmoid(o) * c.array().tanh();
        }

        auto& operator()(const MatrixXf& X)
        {
            const size_t T = X.rows();
            const size_t H = whh.cols();
            out.resize(T, H);

            // Zero hidden state and cell state
            h.setZero();
            c.setZero();

            // Precompute input projection for all timesteps
            xw.resize(T, 4*H);
            xw.noalias() = X * wih.transpose();
            xw.rowwise() += bias.transpose();

            for (size_t t{0}; t < T; ++t)
            {
                // gates = xw[t] + whh * h
                gates.noalias() = xw.row(t).transpose() + whh * h;
                apply_gates();
                out.row(t) = h.transpose();
            }

            return out;
        }   
    };

//----------------------------------------------------------------------------------------------------------------

    struct encodec_lstm
    {
        lstm_cell   cells[2];
        MatrixXf    out;

        encodec_lstm(size_t dim)
        : cells{lstm_cell(dim, dim), lstm_cell(dim, dim)}
        {
        }

        auto load_weights(std::span<const float> data) -> std::span<const float>
        {
            data = cells[0].load_weights(data);
            data = cells[1].load_weights(data);
            return data;
        }

        std::span<float> operator()(std::span<const float> input)
        {
            const size_t C = cells[0].wih.cols();
            const size_t T = input.size() / C;
            out.resize(T, C);
            auto X = Eigen::Map<const MatrixXf>(input.data(), T, C);
            auto& Y = cells[0](X);
            auto& Z = cells[1](Y);
            out.noalias() = X + Z;
            return std::span{out.data(), (size_t)out.size()};
        }
    };

//----------------------------------------------------------------------------------------------------------------

    struct resnet_block
    {
        elu_layer   a0;
        elu_layer   a1;
        conv        b0;
        linear      b1;
        linear      b2;

        resnet_block(size_t c, bool causal=true, bool normalized=false)
        : a0(false),
          a1(true),
          b0(c,   c/2, 3, 1, causal, normalized),
          b1(c/2, c, normalized),
          b2(c,   c, normalized)
        {}

        auto load_weights(std::span<const float> data) -> std::span<const float>
        {
            data = b0.load_weights(data);
            data = b1.load_weights(data);
            data = b2.load_weights(data);
            return data;
        }

        std::span<float> operator()(std::span<float> input)
        {
            auto x = b1(a1(b0(a0(input))));
            auto y = b2(input);
            add(x, y, y);
            return y;
        }
    };

//----------------------------------------------------------------------------------------------------------------

    struct encoder_block
    {
        resnet_block b0;
        elu_layer    a1;
        conv         b1;

        encoder_block(size_t c1, size_t c2, size_t s,
                      bool causal=true, bool normalized=false)
        : b0(c1, causal, normalized),
          a1(true),
          b1(c1, c2, s*2, s, causal, normalized)
        {}

        auto load_weights(std::span<const float> data) -> std::span<const float>
        {
            data = b0.load_weights(data);
            data = b1.load_weights(data);
            return data;
        }

        std::span<float> operator()(std::span<float> input)
        {
            return b1(a1(b0(input)));
        }
    };

//----------------------------------------------------------------------------------------------------------------

    struct decoder_block
    {
        elu_layer       a0;
        conv_transpose  b0;
        resnet_block    b1;

        decoder_block(size_t c1, size_t c2, size_t s,
                      bool causal=true, bool normalized=false)
        : a0(true),
          b0(c1, c2, s*2, s, causal, normalized),
          b1(c2, causal, normalized)
        {}

        auto load_weights(std::span<const float> data) -> std::span<const float>
        {
            data = b0.load_weights(data);
            data = b1.load_weights(data);
            return data;
        }

        std::span<float> operator()(std::span<float> input)
        {
            return b1(b0(a0(input)));
        }
    };

//----------------------------------------------------------------------------------------------------------------

    struct encoder::impl
    {
        runtime_model model;
        model_info model_info_;
        conv          b0;
        encoder_block b1;
        encoder_block b2;
        encoder_block b3;
        encoder_block b4;
        encodec_lstm  b5;
        elu_layer     a6;
        conv          b6;
        rvq           rvq_;
        std::vector<float> normalized_audio;

        impl()
        : model_info_{24000, 1, NLEVELS, true, false},
          b0(  1,  32, 7),
          b1( 32,  64, 2, model_info_.causal, model_info_.normalized),
          b2( 64, 128, 4, model_info_.causal, model_info_.normalized),
          b3(128, 256, 5, model_info_.causal, model_info_.normalized),
          b4(256, 512, 8, model_info_.causal, model_info_.normalized),
          b5(512),
          a6(true),
          b6(512, 128, 7)
        {
            auto weights = std::span{ENCODER_WEIGHTS, ENCODER_SIZE};
            weights = b0.load_weights(weights);
            weights = b1.load_weights(weights);
            weights = b2.load_weights(weights);
            weights = b3.load_weights(weights);
            weights = b4.load_weights(weights);
            weights = b5.load_weights(weights);
            weights = b6.load_weights(weights);
            if (!weights.empty()) throw std::runtime_error("Failed to load encoder weights");
        }

        explicit impl(runtime_model model_)
        : model{std::move(model_)},
          model_info_{model.info},
          b0(model_info_.channels, 32, 7, 1, model_info_.causal, model_info_.normalized),
          b1( 32,  64, 2, model_info_.causal, model_info_.normalized),
          b2( 64, 128, 4, model_info_.causal, model_info_.normalized),
          b3(128, 256, 5, model_info_.causal, model_info_.normalized),
          b4(256, 512, 8, model_info_.causal, model_info_.normalized),
          b5(512),
          a6(true),
          b6(512, 128, 7, 1, model_info_.causal, model_info_.normalized),
          rvq_(model.rvq_weights, model_info_.max_quantizers)
        {
            if (model.encoder_weights.empty())
                throw std::runtime_error("This model file does not contain encoder weights");
            auto weights = std::span<const float>{model.encoder_weights};
            weights = b0.load_weights(weights);
            weights = b1.load_weights(weights);
            weights = b2.load_weights(weights);
            weights = b3.load_weights(weights);
            weights = b4.load_weights(weights);
            weights = b5.load_weights(weights);
            weights = b6.load_weights(weights);
            if (!weights.empty()) throw std::runtime_error("Unexpected trailing encoder weights");
            std::vector<float>().swap(model.encoder_weights);
            std::vector<float>().swap(model.decoder_weights);
        }

        encoded_frame encode_frame(std::span<const float> audio, unsigned int num_quantizers,
                                   size_t beam_size = RVQ_BEAM_SIZE)
        {
            (void)cpu_threads();
            if (num_quantizers < 1 || num_quantizers > model_info_.max_quantizers)
                throw std::runtime_error("Invalid number of quantizers");
            if (audio.empty() || audio.size() % model_info_.channels != 0)
                throw std::runtime_error("Audio does not contain complete input frames");

            float scale = 1.0f;
            std::span<const float> encoder_input = audio;
            if (model_info_.normalized)
            {
                const size_t frames = audio.size() / model_info_.channels;
                double energy = 0.0;
                for (size_t frame = 0; frame < frames; ++frame)
                {
                    float mono = 0.0f;
                    for (size_t channel = 0; channel < model_info_.channels; ++channel)
                        mono += audio[frame*model_info_.channels + channel];
                    mono /= float(model_info_.channels);
                    energy += double(mono) * double(mono);
                }
                scale = float(std::sqrt(energy / double(frames)));
                const float divisor = scale + 1.0e-8f;
                normalized_audio.resize(audio.size());
                for (size_t i = 0; i < audio.size(); ++i) normalized_audio[i] = audio[i] / divisor;
                encoder_input = normalized_audio;
            }

            auto x = b0(encoder_input);
            x      = b1(x);
            x      = b2(x);
            x      = b3(x);
            x      = b4(x);
            x      = b5(x);
            x      = b6(a6(x));
            const size_t code_frames = x.size() / CODEBOOK_DIM;
            return {rvq_.encode(x, num_quantizers, beam_size), code_frames, scale};
        }

        std::span<const uint8_t> encode(std::span<const float> audio, unsigned int num_quantizers,
                                        size_t beam_size = RVQ_BEAM_SIZE)
        {
            return encode_frame(audio, num_quantizers, beam_size).packet;
        }
    };

//----------------------------------------------------------------------------------------------------------------

    struct decoder::impl
    {
        runtime_model model;
        model_info model_info_;
        conv          b0;
        encodec_lstm  b1;
        decoder_block b2;
        decoder_block b3;
        decoder_block b4;
        decoder_block b5;
        elu_layer     a6;
        conv          b6;
        rvq           rvq_;

        impl()
        : model_info_{24000, 1, NLEVELS, true, false},
          b0(128, 512, 7),
          b1(512),
          b2(512, 256, 8),
          b3(256, 128, 5),
          b4(128,  64, 4),
          b5( 64,  32, 2),
          a6(true),
          b6( 32,   1, 7)
        {
            auto weights = std::span{DECODER_WEIGHTS, DECODER_SIZE};
            weights = b0.load_weights(weights);
            weights = b1.load_weights(weights);
            weights = b2.load_weights(weights);
            weights = b3.load_weights(weights);
            weights = b4.load_weights(weights);
            weights = b5.load_weights(weights);
            weights = b6.load_weights(weights);
            if (!weights.empty()) throw std::runtime_error("Failed to load decoder weights");
        }

        explicit impl(runtime_model model_)
        : model{std::move(model_)},
          model_info_{model.info},
          b0(128, 512, 7, 1, model_info_.causal, model_info_.normalized),
          b1(512),
          b2(512, 256, 8, model_info_.causal, model_info_.normalized),
          b3(256, 128, 5, model_info_.causal, model_info_.normalized),
          b4(128,  64, 4, model_info_.causal, model_info_.normalized),
          b5( 64,  32, 2, model_info_.causal, model_info_.normalized),
          a6(true),
          b6( 32, model_info_.channels, 7, 1, model_info_.causal, model_info_.normalized),
          rvq_(model.rvq_weights, model_info_.max_quantizers)
        {
            auto weights = std::span<const float>{model.decoder_weights};
            weights = b0.load_weights(weights);
            weights = b1.load_weights(weights);
            weights = b2.load_weights(weights);
            weights = b3.load_weights(weights);
            weights = b4.load_weights(weights);
            weights = b5.load_weights(weights);
            weights = b6.load_weights(weights);
            if (!weights.empty()) throw std::runtime_error("Unexpected trailing decoder weights");
            // Layer objects own their decoder tensors after loading. Retaining the
            // serialized copy would waste roughly 28 MiB for every frame worker.
            std::vector<float>().swap(model.decoder_weights);
            std::vector<float>().swap(model.encoder_weights);
        }

        std::span<const float> decode(std::span<const uint8_t> packet, unsigned int num_quantizers,
                                      size_t code_frames = 0)
        {
            (void)cpu_threads();
            if (num_quantizers < 1 || num_quantizers > model_info_.max_quantizers)
                throw std::runtime_error("Invalid number of quantizers");

            auto x  = rvq_.decode(packet, num_quantizers, code_frames);
            x       = b0(x);
            x       = b1(x);
            x       = b2(x);
            x       = b3(x);
            x       = b4(x);
            x       = b5(x);
            x       = b6(a6(x));
            return x;
        }
    };
    
//----------------------------------------------------------------------------------------------------------------

    encoder::encoder() : state{std::make_unique<impl>()} {}
    encoder::encoder(const std::filesystem::path& model_path)
    : state{std::make_unique<impl>(load_runtime_model(model_path))} {}
    encoder::~encoder()                          = default;
    encoder::encoder(encoder&& other)            = default;
    encoder& encoder::operator=(encoder&& other) = default;

    std::span<const uint8_t> encoder::encode(std::span<const float> audio, unsigned int num_quantizers)
    {
        return state->encode(audio, num_quantizers);
    }

    std::span<const uint8_t> encoder::encode(std::span<const float> audio,
                                             unsigned int num_quantizers,
                                             std::size_t beam_size)
    {
        return state->encode(audio, num_quantizers, beam_size);
    }

    encoded_frame encoder::encode_frame(std::span<const float> audio, unsigned int num_quantizers)
    {
        return state->encode_frame(audio, num_quantizers);
    }

    encoded_frame encoder::encode_frame(std::span<const float> audio,
                                        unsigned int num_quantizers,
                                        std::size_t beam_size)
    {
        return state->encode_frame(audio, num_quantizers, beam_size);
    }

    model_info encoder::info() const { return state->model_info_; }

//----------------------------------------------------------------------------------------------------------------

    decoder::decoder() : state{std::make_unique<impl>()} {}
    decoder::decoder(const std::filesystem::path& model_path)
    : state{std::make_unique<impl>(load_runtime_model(model_path))} {}
    decoder::~decoder()                          = default;
    decoder::decoder(decoder&& other)            = default;
    decoder& decoder::operator=(decoder&& other) = default;

    std::span<const float> decoder::decode(std::span<const uint8_t> packet, unsigned int num_quantizers)
    {
        return state->decode(packet, num_quantizers);
    }

    std::span<const float> decoder::decode(std::span<const uint8_t> packet,
                                           unsigned int num_quantizers,
                                           std::size_t code_frames)
    {
        return state->decode(packet, num_quantizers, code_frames);
    }

    model_info decoder::info() const { return state->model_info_; }

//----------------------------------------------------------------------------------------------------------------

}
