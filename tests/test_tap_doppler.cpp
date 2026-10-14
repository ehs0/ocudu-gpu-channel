// Per-tap Doppler of runtime profiles (docs/plans/sionna-tap-doppler.md),
// checked against closed-form output on both backends.
//
// The input is a constant 1 + 0j and every tap sits on an integer delay, so the
// windowed-sinc filter is a unit impulse and each output sample is exactly the
// sum of the tap weights at that sample:
//   y[n] = sum_k g_k exp(j (phi_k + 2 pi f_k t(n))),  t(n) = (n - anchor) / fs
// for an unstamped profile. The checks compare every sample with that sum, so a
// wrong slope, a wrong time origin, a slot-boundary jump or a missing beat all
// fail. Tolerances are float rounding of the angle and of the convolution.

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/physical_link.h"
#include "ocudu_gpu_channel/processing.h"

#include <chrono>
#include <cmath>
#include <complex>
#include <cstdio>
#include <memory>
#include <numbers>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

constexpr std::uint64_t kFs = 23040000;
constexpr std::size_t kCount = 2304;  // 0.1 ms slots
constexpr double kTwoPi = 2.0 * std::numbers::pi;

void require(bool ok, const std::string& message)
{
  if (!ok) throw std::runtime_error(message);
}

const char* name_of(ocg::Backend backend)
{
  return backend == ocg::Backend::Cpu ? "cpu" : "cuda";
}

struct Tap {
  double delay;
  double gain_db;
  double phase;
  double doppler;
};

struct Fixture {
  ocg::TopologyConfig config;
  std::unique_ptr<ocg::ChannelProcessor> processor;
  ocg::BrokerLinkControl* control = nullptr;
  ocg::IqBuffer input = ocg::IqBuffer(kCount, ocg::IqSample{1.0F, 0.0F});
  std::vector<ocg::SuperpositionInput> lanes;

  explicit Fixture(ocg::Backend backend)
  {
    config.runtime.backend = backend;
    config.runtime.batch_samples_auto = false;
    config.runtime.batch_samples = kCount;
    config.runtime.queue_samples = kCount * 8;
    int port = 5800;
    for (const char* name : {"tx", "rx"}) {
      ocg::RadioNodeConfig node;
      node.id = name;
      ocg::DeviceConfig device;
      device.id = std::string(name) + "0";
      device.sample_rate_hz = kFs;
      device.tx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
      device.rx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
      node.tx_ports.push_back(device.id);
      node.rx_ports.push_back(device.id);
      config.devices.push_back(device);
      config.radio_nodes.push_back(node);
    }
    ocg::ModelConfig model;
    model.id = "rt";
    model.chain.push_back({.type = ocg::ModelStepType::Tdl,
                           .params = {},
                           .taps = {{.delay_samples = 0.0}},
                           .taps_declared = true});
    config.models.emplace(model.id, model);
    config.links = {{.from = "tx", .to = "rx", .model = model.id},
                    {.from = "rx", .to = "tx", .model = model.id}};
    require(ocg::validate_config(config).empty(), "fixture must validate");
    processor = ocg::create_channel_processor(config);
    for (const auto& lane : ocg::resolve_topology(config).lanes) {
      if (lane.dst_node != "rx") continue;
      lanes.push_back({.link_key = lane.key,
                       .model = ocg::find_model(config, lane.model_id),
                       .samples = input,
                       .rx_port = lane.rx_port,
                       .tx_port = lane.tx_port});
    }
    control = processor->collect_control_links().at("tx>rx:rt");
  }

  void swap(const std::vector<Tap>& taps, std::uint64_t phase_time_unix_ns = 0)
  {
    ocg::MatrixProfileShadow m{};
    m.nt = m.nr = m.lane_count = 1;
    m.phase_time_unix_ns = phase_time_unix_ns;
    m.lanes[0].n_taps = static_cast<int>(taps.size());
    for (std::size_t k = 0; k < taps.size(); ++k) {
      m.lanes[0].taps[k] = {.delay_samples = taps[k].delay,
                            .gain_db = taps[k].gain_db,
                            .phase_rad = taps[k].phase,
                            .doppler_hz = taps[k].doppler};
    }
    control->shadow_matrix_profile = m;
    control->matrix_profile_pending = true;
    control->profile_pending = false;
    control->seqno.fetch_add(1, std::memory_order_release);
  }

  ocg::IqBuffer run()
  {
    ocg::IqBuffer out(kCount);
    std::vector<std::span<ocg::IqSample>> rows{std::span<ocg::IqSample>(out)};
    processor->process_superposition("rx", lanes, nullptr, kFs, rows);
    return out;
  }
};

std::complex<double> expected(const std::vector<Tap>& taps, double t)
{
  std::complex<double> sum{};
  for (const auto& tap : taps) {
    sum += std::polar(std::pow(10.0, tap.gain_db / 20.0), tap.phase + kTwoPi * tap.doppler * t);
  }
  return sum;
}

// Every sample of `slots` slots against the closed form, sample n of the run
// at t = (n - anchor) / fs. Returns the worst error.
double check_against(Fixture& f, const std::vector<Tap>& taps, std::uint64_t first_sample,
                     std::uint64_t anchor, int slots, std::vector<std::complex<double>>* trace)
{
  double worst = 0.0;
  for (int slot = 0; slot < slots; ++slot) {
    const auto out = f.run();
    for (std::size_t i = 0; i < kCount; ++i) {
      const std::uint64_t n = first_sample + static_cast<std::uint64_t>(slot) * kCount + i;
      const double t = static_cast<double>(n - anchor) / static_cast<double>(kFs);
      const std::complex<double> want = expected(taps, t);
      const std::complex<double> got{out[i].i, out[i].q};
      worst = std::max(worst, std::abs(got - want));
      if (trace != nullptr) trace->push_back(got);
    }
  }
  return worst;
}

// 1: one tap's output phase advances at exactly 2 pi f_D per second.
void single_tap_slope(ocg::Backend backend)
{
  Fixture f(backend);
  const std::vector<Tap> taps = {{0.0, 0.0, 0.3, 85.0}};
  f.swap(taps);
  std::vector<std::complex<double>> trace;
  // 120 slots = 12 ms, about one full turn at 85 Hz.
  const double worst = check_against(f, taps, 0, 0, 120, &trace);
  require(worst < 2e-5, std::string(name_of(backend)) + ": single tap deviates from g e^{j(phi + 2 pi f t)}");
  // The slope itself, by least squares over the unwrapped phase.
  double sx = 0, sy = 0, sxx = 0, sxy = 0, unwrapped = std::arg(trace[0]), previous = unwrapped;
  for (std::size_t n = 0; n < trace.size(); ++n) {
    const double a = std::arg(trace[n]);
    double d = a - previous;
    d -= kTwoPi * std::round(d / kTwoPi);
    unwrapped = n == 0 ? a : unwrapped + d;
    previous = a;
    const double x = static_cast<double>(n) / static_cast<double>(kFs);
    sx += x; sy += unwrapped; sxx += x * x; sxy += x * unwrapped;
  }
  const double m = static_cast<double>(trace.size());
  const double slope_hz = (m * sxy - sx * sy) / (m * sxx - sx * sx) / kTwoPi;
  require(std::abs(slope_hz - 85.0) < 1e-3,
          std::string(name_of(backend)) + ": measured slope " + std::to_string(slope_hz) + " Hz");
  std::printf("%s single tap: max sample error %.2e, slope %.6f Hz (expected 85)\n",
              name_of(backend), worst, slope_hz);
}

// 4: two taps with different Doppler beat at 1/|f1 - f2|, which a link-wide
// rotation (CFO) cannot produce: |y|^2 would stay constant.
void two_tap_beat(ocg::Backend backend)
{
  Fixture f(backend);
  const std::vector<Tap> taps = {{0.0, 0.0, 0.0, 85.0}, {3.0, -2.0, 1.0, -40.0}};
  f.swap(taps);
  std::vector<std::complex<double>> trace;
  // 250 slots = 25 ms, about three beat periods of 8 ms.
  // Tap 2 reads three samples of history; the first slot's history is the
  // zero-filled ring after the reset, so the comparison starts at slot 1.
  f.run();
  const double worst = check_against(f, taps, kCount, 0, 249, &trace);
  require(worst < 4e-5, std::string(name_of(backend)) + ": two-tap output deviates from the closed form");
  // Beat period from the envelope minima.
  std::vector<double> power(trace.size());
  for (std::size_t n = 0; n < trace.size(); ++n) power[n] = std::norm(trace[n]);
  std::vector<double> minima;
  const std::size_t half = kFs / 125 / 4;  // a quarter period on each side
  for (std::size_t n = half; n + half < power.size(); ++n) {
    bool lowest = true;
    // Strict on the left so a minimum spread over equal samples counts once.
    for (std::size_t k = n - half; k <= n + half && lowest; ++k)
      lowest = k < n ? power[k] > power[n] : power[k] >= power[n];
    if (lowest) minima.push_back(static_cast<double>(n) / static_cast<double>(kFs));
  }
  require(minima.size() >= 2, std::string(name_of(backend)) + ": no beat minima found");
  const double period = (minima.back() - minima.front()) / static_cast<double>(minima.size() - 1);
  // The envelope is flat at its minimum, so float rounding of |y|^2 moves the
  // discrete argmin by a few samples (4 seen); the per-sample check above is
  // the strict one. Allow 8 samples of argmin error on the mean spacing.
  require(std::abs(period - 1.0 / 125.0) < 8.0 / static_cast<double>(kFs),
          std::string(name_of(backend)) + ": beat period " + std::to_string(period));
  std::printf("%s two taps: max sample error %.2e, beat period %.5f ms (expected 8.00000)\n",
              name_of(backend), worst, period * 1e3);
}

// 6: a gain/phase-only update keeps history (no warmup) and re-anchors the
// rotation at its own slot: the new phases hold at that slot, not earlier.
void history_kept_and_reanchored(ocg::Backend backend)
{
  Fixture f(backend);
  std::vector<Tap> taps = {{0.0, 0.0, 0.3, 85.0}, {2.0, -6.0, 0.0, 20.0}};
  f.swap(taps);
  for (int i = 0; i < 10; ++i) f.run();
  const auto resets = ocg::read_telemetry_snapshot(*f.control).warmup_event_seq;
  taps[0].phase = -1.2;
  taps[0].doppler = -60.0;  // Doppler changes too: still the same delays
  taps[1].gain_db = -3.0;
  f.swap(taps);
  const std::uint64_t anchor = 10 * kCount;
  const double worst = check_against(f, taps, anchor, anchor, 40, nullptr);
  require(ocg::read_telemetry_snapshot(*f.control).warmup_event_seq == resets,
          std::string(name_of(backend)) + ": a Doppler/gain/phase update must keep history");
  require(worst < 2e-5, std::string(name_of(backend)) + ": rotation was not re-anchored at the update slot");
}

// A stamped profile starts as old as its phase time: at its first sample the
// tap has already turned by 2 pi f age, age = wall time at the snap - phase time.
void stamped_phase_time(ocg::Backend backend)
{
  Fixture f(backend);
  const auto now_ns = [] {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count());
  };
  const double f_hz = 5.0;
  const std::uint64_t before = now_ns();
  const std::uint64_t phase_time = before - 50'000'000ULL;  // 50 ms old
  f.swap({{0.0, 0.0, 0.0, f_hz}}, phase_time);
  const auto out = f.run();
  const std::uint64_t after = now_ns();
  const double turned = std::arg(std::complex<double>{out[0].i, out[0].q});
  const double low = kTwoPi * f_hz * static_cast<double>(before - phase_time) * 1e-9;
  const double high = kTwoPi * f_hz * static_cast<double>(after - phase_time) * 1e-9;
  require(turned >= low - 1e-5 && turned <= high + 1e-5,
          std::string(name_of(backend)) + ": stamped start phase " + std::to_string(turned) +
              " outside [" + std::to_string(low) + ", " + std::to_string(high) + "]");
  std::printf("%s stamped profile: start phase %.5f rad within [%.5f, %.5f]\n", name_of(backend),
              turned, low, high);
}

// The wall-per-sample rate is the median interval rate, so one stall does not
// set the rate of the following updates.
void rate_window()
{
  ocg::TapRotationClock clock;
  require(ocg::tap_rotation_rate(clock, kFs) == 1.0, "no anchors: rate 1");
  std::uint64_t wall = 1'000'000'000'000ULL;
  std::uint64_t samples = 0;
  for (int k = 0; k < 20; ++k) {
    ocg::anchor_tap_rotation(clock, samples, wall - 30'000'000ULL, wall);
    if (k == 0) require(ocg::tap_rotation_rate(clock, kFs) == 1.0, "one anchor: rate 1");
    samples += 2196480;  // 95.33 ms of samples per 100 ms of wall time
    wall += k == 12 ? 2'100'000'000ULL : 100'000'000ULL;  // one 2 s stall
  }
  const double rate = ocg::tap_rotation_rate(clock, kFs);
  require(std::abs(rate - 100.0 / 95.33333333333333) < 1e-9,
          "median rate ignores the stall: " + std::to_string(rate));
  require(std::abs(clock.age_at_anchor_s - 0.030) < 1e-12, "age is wall at the snap minus phase time");
  ocg::anchor_tap_rotation(clock, samples, 0, 0);
  require(ocg::tap_rotation_rate(clock, kFs) == 1.0 && clock.age_at_anchor_s == 0.0,
          "an unstamped profile runs on sample time");
}

// CPU is the reference: the same Doppler profile gives the same samples on CUDA.
void parity()
{
#if OCUDU_GPU_CHANNEL_HAS_CUDA
  Fixture cpu(ocg::Backend::Cpu);
  Fixture gpu(ocg::Backend::Cuda);
  const std::vector<Tap> taps = {{0.0, 0.0, 0.3, 85.0}, {1.5, -3.0, -0.4, 31.0}, {7.25, -9.0, 2.0, -77.0}};
  cpu.swap(taps);
  gpu.swap(taps);
  double worst = 0.0;
  for (int slot = 0; slot < 50; ++slot) {
    const auto a = cpu.run();
    const auto b = gpu.run();
    for (std::size_t i = 0; i < kCount; ++i) {
      worst = std::max(worst, static_cast<double>(std::hypot(a[i].i - b[i].i, a[i].q - b[i].q)));
    }
  }
  require(worst < 1e-4, "CPU/CUDA Doppler outputs differ by " + std::to_string(worst));
  std::printf("cpu/cuda parity with fractional delays: max difference %.2e\n", worst);
#endif
}

} // namespace

int main()
{
  try {
    std::vector<ocg::Backend> backends = {ocg::Backend::Cpu};
#if OCUDU_GPU_CHANNEL_HAS_CUDA
    backends.push_back(ocg::Backend::Cuda);
#endif
    rate_window();
    for (const auto backend : backends) {
      single_tap_slope(backend);
      two_tap_beat(backend);
      history_kept_and_reanchored(backend);
      stamped_phase_time(backend);
    }
    parity();
  } catch (const std::exception& e) {
    std::fprintf(stderr, "FAIL: %s\n", e.what());
    return 1;
  }
  std::printf("tap_doppler: all checks passed\n");
  return 0;
}
