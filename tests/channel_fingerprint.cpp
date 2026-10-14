// Output fingerprints of fixed channel fixtures, for bit-exact A/B comparison
// of two revisions: build this same file against each and diff the output.
//
// Every fixture is deterministic (fixed inputs, fixed seeds, fixed control
// updates) and runs on each available backend. A line is
//   <fixture> <backend> <FNV-1a 64 of every output sample's float bytes>
// No fixture sets a tap Doppler or a phase time, so a revision that only adds
// those features must print identical lines.

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/physical_link.h"
#include "ocudu_gpu_channel/processing.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <span>
#include <string>
#include <vector>

namespace {

constexpr std::uint64_t kSampleRate = 23040000;
constexpr std::size_t kCount = 2304;

std::uint64_t fnv(std::uint64_t h, const void* data, std::size_t bytes)
{
  const auto* p = static_cast<const unsigned char*>(data);
  for (std::size_t i = 0; i < bytes; ++i) {
    h ^= p[i];
    h *= 1099511628211ULL;
  }
  return h;
}

struct Lcg {
  std::uint64_t s;
  float next()
  {
    s = s * 6364136223846793005ULL + 1442695040888963407ULL;
    return static_cast<float>(static_cast<std::int32_t>(s >> 32)) / 2147483648.0F;
  }
};

ocg::TopologyConfig topology(ocg::Backend backend, int nt, int nr, const ocg::ModelStep& step)
{
  ocg::TopologyConfig config;
  config.runtime.backend = backend;
  config.runtime.batch_samples_auto = false;
  config.runtime.batch_samples = kCount;
  config.runtime.queue_samples = kCount * 8;
  int port = 4800;
  for (const auto& [name, count] : {std::pair{"tx", nt}, std::pair{"rx", nr}}) {
    ocg::RadioNodeConfig node;
    node.id = name;
    for (int p = 0; p < count; ++p) {
      ocg::DeviceConfig device;
      device.id = std::string(name) + std::to_string(p);
      device.sample_rate_hz = kSampleRate;
      device.tx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
      device.rx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
      node.tx_ports.push_back(device.id);
      node.rx_ports.push_back(device.id);
      config.devices.push_back(device);
    }
    config.radio_nodes.push_back(node);
  }
  ocg::ModelConfig model;
  model.id = "m";
  model.chain.push_back(step);
  config.models.emplace(model.id, model);
  config.links = {{.from = "tx", .to = "rx", .model = model.id},
                  {.from = "rx", .to = "tx", .model = model.id}};
  return config;
}

// Runs `slots` slots into rx; `update(slot, control)` may stage a control
// change before the slot. Returns the fingerprint of all rx output.
template <class Update>
std::uint64_t run(ocg::TopologyConfig config, int slots, Update update)
{
  if (!ocg::validate_config(config).empty()) {
    std::fprintf(stderr, "fixture does not validate\n");
    std::exit(2);
  }
  auto processor = ocg::create_channel_processor(config);
  int nt = 0;
  int nr = 0;
  for (const auto& node : config.radio_nodes) {
    if (node.id == "tx") nt = static_cast<int>(node.tx_ports.size());
    if (node.id == "rx") nr = static_cast<int>(node.rx_ports.size());
  }
  std::vector<ocg::IqBuffer> inputs(static_cast<std::size_t>(nt), ocg::IqBuffer(kCount));
  std::vector<ocg::SuperpositionInput> lanes;
  for (const auto& lane : ocg::resolve_topology(config).lanes) {
    if (lane.dst_node != "rx") continue;
    lanes.push_back({.link_key = lane.key,
                     .model = ocg::find_model(config, lane.model_id),
                     .samples = inputs[static_cast<std::size_t>(lane.tx_port)],
                     .rx_port = lane.rx_port,
                     .tx_port = lane.tx_port});
  }
  auto controls = processor->collect_control_links();
  ocg::BrokerLinkControl* control = controls.count("tx>rx:m") ? controls.at("tx>rx:m") : nullptr;
  Lcg rng{12345};
  std::uint64_t h = 1469598103934665603ULL;
  std::vector<ocg::IqBuffer> output(static_cast<std::size_t>(nr), ocg::IqBuffer(kCount));
  for (int slot = 0; slot < slots; ++slot) {
    for (auto& row : inputs)
      for (auto& s : row) s = {rng.next(), rng.next()};
    if (control != nullptr) update(slot, *control);
    std::vector<std::span<ocg::IqSample>> rows;
    for (auto& row : output) rows.emplace_back(row);
    processor->process_superposition("rx", lanes, nullptr, kSampleRate, rows);
    for (const auto& row : output) h = fnv(h, row.data(), row.size() * sizeof(ocg::IqSample));
  }
  return h;
}

ocg::ModelStep tdl_step(bool fading)
{
  ocg::ModelStep step{.type = ocg::ModelStepType::Tdl, .params = {}, .taps_declared = true};
  step.taps = {{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.3},
               {.delay_samples = 3.25, .gain_db = -4.0, .phase_rad = -1.1},
               {.delay_samples = 11.5, .gain_db = -9.0, .phase_rad = 2.0}};
  if (fading) {
    step.taps[0].is_los = true;
    step.taps[0].los_k_db = 6.0;
    step.taps[0].los_angle_rad = 0.7;
    step.fading_enabled = true;
    step.fading_f_d_max_hz = 85.0;
    step.fading_grid_us = 100.0;
  }
  return step;
}

void matrix_updates(int slot, ocg::BrokerLinkControl& control, int nt, int nr, bool delays_move)
{
  if (slot % 5 != 0) return;
  ocg::MatrixProfileShadow m{};
  m.nt = nt;
  m.nr = nr;
  m.lane_count = nt * nr;
  for (int lane = 0; lane < m.lane_count; ++lane) {
    auto& p = m.lanes[lane];
    p.n_taps = 4;
    for (int k = 0; k < p.n_taps; ++k) {
      p.taps[k].delay_samples = 2.0 * k + 0.0625 * lane + (delays_move ? 0.0625 * (slot / 5) : 0.0);
      p.taps[k].gain_db = -3.0 * k - 0.5 * (slot % 7);
      p.taps[k].phase_rad = 0.37 * (k + 1) * (lane + 1) + 0.11 * slot;
    }
  }
  control.shadow_matrix_profile = m;
  control.matrix_profile_pending = true;
  control.profile_pending = false;
  control.seqno.fetch_add(1, std::memory_order_release);
}

void scalar_updates(int slot, ocg::BrokerLinkControl& control)
{
  if (slot % 6 != 0) return;
  ocg::ProfileShadow p{};
  p.n_taps = 3;
  for (int k = 0; k < p.n_taps; ++k) {
    p.taps[k].delay_samples = 1.5 * k + 0.0625 * (slot / 6);
    p.taps[k].gain_db = -2.0 * k;
    p.taps[k].phase_rad = 0.5 * k + 0.07 * slot;
  }
  control.shadow_profile = p;
  control.profile_pending = true;
  control.matrix_profile_pending = false;
  control.seqno.fetch_add(1, std::memory_order_release);
}

} // namespace

int main()
{
  std::vector<ocg::Backend> backends = {ocg::Backend::Cpu};
#if OCUDU_GPU_CHANNEL_HAS_CUDA
  backends.push_back(ocg::Backend::Cuda);
#endif
  for (const auto backend : backends) {
    const char* name = backend == ocg::Backend::Cpu ? "cpu" : "cuda";
    const auto none = [](int, ocg::BrokerLinkControl&) {};
    std::printf("tdl_static_1x1 %s %016llx\n", name,
                static_cast<unsigned long long>(run(topology(backend, 1, 1, tdl_step(false)), 40, none)));
    std::printf("tdl_fading_los_1x1 %s %016llx\n", name,
                static_cast<unsigned long long>(run(topology(backend, 1, 1, tdl_step(true)), 40, none)));
    std::printf("tdl_fading_2x2 %s %016llx\n", name,
                static_cast<unsigned long long>(run(topology(backend, 2, 2, tdl_step(true)), 40, none)));
    std::printf("matrix_gain_phase_1x1 %s %016llx\n", name,
                static_cast<unsigned long long>(run(topology(backend, 1, 1, tdl_step(false)), 60,
                    [](int s, ocg::BrokerLinkControl& c) { matrix_updates(s, c, 1, 1, false); })));
    std::printf("matrix_moving_delays_2x1 %s %016llx\n", name,
                static_cast<unsigned long long>(run(topology(backend, 2, 1, tdl_step(false)), 60,
                    [](int s, ocg::BrokerLinkControl& c) { matrix_updates(s, c, 2, 1, true); })));
    std::printf("scalar_profile_1x1 %s %016llx\n", name,
                static_cast<unsigned long long>(run(topology(backend, 1, 1, tdl_step(false)), 60,
                    [](int s, ocg::BrokerLinkControl& c) { scalar_updates(s, c); })));
  }
  return 0;
}
