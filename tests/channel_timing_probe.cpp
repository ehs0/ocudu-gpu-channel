// Slot-processing time with Sionna-style profile updates, for A/B comparison
// of two revisions or of tap Doppler on and off on one revision.
//
//   channel_timing_probe <profiles.txt> <backend cpu|cuda> <slots> [updates_per_s]
//
// profiles.txt: one profile per line, "n d0 g0 p0 f0 d1 g1 p1 f1 ...", taps as
// delay_samples gain_db phase_rad doppler_hz. One 1x1 link gnb>ue, 23.04 MS/s,
// 23040-sample (1 ms) slots, back to back (not paced); a new profile every
// 1000/updates_per_s slots, cycling through the file. Prints process call time
// percentiles, the share of calls over the 1 ms slot, and the warmups (history
// resets) the profile sequence caused. A revision without tap Doppler ignores
// the Doppler column.

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/physical_link.h"
#include "ocudu_gpu_channel/processing.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <fstream>
#include <sstream>
#include <span>
#include <string>
#include <vector>

namespace {

constexpr std::uint64_t kFs = 23040000;
constexpr std::size_t kCount = 23040;

struct Tap {
  double delay, gain, phase, doppler;
};

template <class T>
void set_doppler(T& tap, double value)
{
  if constexpr (requires { tap.doppler_hz; }) {
    tap.doppler_hz = value;
  }
}

} // namespace

int main(int argc, char** argv)
{
  if (argc < 4) {
    std::fprintf(stderr, "usage: %s profiles.txt cpu|cuda slots [updates_per_s]\n", argv[0]);
    return 2;
  }
  std::vector<std::vector<Tap>> profiles;
  std::ifstream in(argv[1]);
  for (std::string line; std::getline(in, line);) {
    std::istringstream row(line);
    int n = 0;
    row >> n;
    std::vector<Tap> taps(static_cast<std::size_t>(n));
    for (auto& t : taps) row >> t.delay >> t.gain >> t.phase >> t.doppler;
    profiles.push_back(taps);
  }
  const ocg::Backend backend = std::string(argv[2]) == "cpu" ? ocg::Backend::Cpu : ocg::Backend::Cuda;
  const int slots = std::stoi(argv[3]);
  const double updates_per_s = argc > 4 ? std::stod(argv[4]) : 10.0;
  const int update_every = static_cast<int>(1000.0 / updates_per_s);

  ocg::TopologyConfig config;
  config.runtime.backend = backend;
  config.runtime.batch_samples_auto = false;
  config.runtime.batch_samples = kCount;
  config.runtime.queue_samples = kCount * 8;
  int port = 6800;
  for (const char* name : {"gnb", "ue"}) {
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
  model.id = "sionna_rt";
  model.chain.push_back({.type = ocg::ModelStepType::Tdl, .params = {}, .taps = {{.delay_samples = 0.0}},
                         .taps_declared = true});
  config.models.emplace(model.id, model);
  config.links = {{.from = "gnb", .to = "ue", .model = model.id}, {.from = "ue", .to = "gnb", .model = model.id}};
  auto processor = ocg::create_channel_processor(config);
  ocg::IqBuffer input(kCount);
  std::uint64_t s = 1;
  for (auto& x : input) {
    s = s * 6364136223846793005ULL + 1442695040888963407ULL;
    x = {static_cast<float>(static_cast<std::int32_t>(s >> 32)) / 2147483648.0F, 0.25F};
  }
  std::vector<ocg::SuperpositionInput> lanes;
  for (const auto& lane : ocg::resolve_topology(config).lanes) {
    if (lane.dst_node != "ue") continue;
    lanes.push_back({.link_key = lane.key, .model = ocg::find_model(config, lane.model_id), .samples = input,
                     .rx_port = lane.rx_port, .tx_port = lane.tx_port});
  }
  auto* control = processor->collect_control_links().at("gnb>ue:sionna_rt");
  ocg::IqBuffer out(kCount);
  std::vector<double> us;
  us.reserve(static_cast<std::size_t>(slots));
  std::size_t next_profile = 0;
  for (int slot = 0; slot < slots + 200; ++slot) {
    if (slot % update_every == 0) {
      const auto& taps = profiles[next_profile++ % profiles.size()];
      ocg::MatrixProfileShadow m{};
      m.nt = m.nr = m.lane_count = 1;
      m.lanes[0].n_taps = static_cast<int>(taps.size());
      for (std::size_t k = 0; k < taps.size(); ++k) {
        auto& t = m.lanes[0].taps[k];
        t.delay_samples = taps[k].delay;
        t.gain_db = taps[k].gain;
        t.phase_rad = taps[k].phase;
        set_doppler(t, taps[k].doppler);
      }
      control->shadow_matrix_profile = m;
      control->matrix_profile_pending = true;
      control->seqno.fetch_add(1, std::memory_order_release);
    }
    std::vector<std::span<ocg::IqSample>> rows{std::span<ocg::IqSample>(out)};
    const auto t0 = std::chrono::steady_clock::now();
    processor->process_superposition("ue", lanes, nullptr, kFs, rows);
    const auto t1 = std::chrono::steady_clock::now();
    if (slot >= 200) us.push_back(std::chrono::duration<double, std::micro>(t1 - t0).count());
  }
  std::sort(us.begin(), us.end());
  const auto at = [&](double q) { return us[std::min(us.size() - 1, static_cast<std::size_t>(q * (us.size() - 1)))]; };
  const auto over = std::count_if(us.begin(), us.end(), [](double v) { return v > 1000.0; });
  std::printf("{\"backend\":\"%s\",\"slots\":%zu,\"p50_us\":%.1f,\"p90_us\":%.1f,\"p99_us\":%.1f,\"p999_us\":%.1f,"
              "\"max_us\":%.1f,\"over_1ms\":%lld,\"warmups\":%llu}\n",
              argv[2], us.size(), at(0.5), at(0.9), at(0.99), at(0.999), us.back(), static_cast<long long>(over),
              static_cast<unsigned long long>(ocg::read_telemetry_snapshot(*control).warmup_event_seq));
  return 0;
}
