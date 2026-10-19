// Sample-indexed control (single-cell scheduler benchmark).
//
//   1. batch_commit's take_effect_at_sample / tag / noise_epoch reach the link
//      control block, are validated, and link_clock reports the link state.
//   2. A gated update takes effect at the first slot that starts at or after
//      its target sample -- not at the next call -- on both backends.
//   3. CUDA receiver noise restarted by reset_receiver_noise() is the same draw
//      sequence as a fresh broker's, whatever the call sizes; the CPU
//      reference says it cannot do this rather than pretending.

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/control_server.h"
#include "ocudu_gpu_channel/physical_link.h"
#include "ocudu_gpu_channel/processing.h"

#include <cmath>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

void require(bool ok, const std::string& message)
{
  if (!ok) {
    std::cerr << "FAIL: " << message << "\n";
    std::exit(1);
  }
}

bool contains(const std::string& haystack, const std::string& needle)
{
  return haystack.find(needle) != std::string::npos;
}

const char* kMatrix = R"({"type":"matrix_profile_swap","link_id":"tx>rx:dynamic","batch_id":"B",)"
                      R"("nt":1,"nr":1,"lanes":[{"rx_port":0,"tx_port":0,)"
                      R"("taps":[{"delay_samples":0.0,"gain_db":-3.5,"phase_rad":0.25}]}]})";

// One TX node, one RX node, one 1x1 link "tx>rx:dynamic", batch 8 samples,
// optionally with an absolute AWGN receiver floor on the RX port.
struct Fixture {
  ocg::TopologyConfig config;
  std::unique_ptr<ocg::ChannelProcessor> processor;
  ocg::BrokerLinkControl* control = nullptr;
  ocg::IqBuffer input = ocg::IqBuffer(8);
  std::vector<ocg::SuperpositionInput> lanes;
  const ocg::ModelConfig* rx_model = nullptr;

  Fixture(ocg::Backend backend, bool noise)
  {
    config.runtime.backend = backend;
    config.runtime.batch_samples_auto = false;
    config.runtime.batch_samples = 8;
    config.runtime.queue_samples = 64;
    int port = 3900;
    for (const char* name : {"tx", "rx"}) {
      ocg::RadioNodeConfig node;
      node.id = name;
      ocg::DeviceConfig device;
      device.id = std::string(name) + "0";
      device.tx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
      device.rx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
      if (noise && std::string(name) == "rx") device.rx_model = "floor";
      node.tx_ports.push_back(device.id);
      node.rx_ports.push_back(device.id);
      config.devices.push_back(device);
      config.radio_nodes.push_back(node);
    }
    ocg::ModelConfig model;
    model.id = "dynamic";
    model.chain.push_back({.type = ocg::ModelStepType::Tdl, .params = {},
                           .taps = {{.delay_samples = 0.0, .gain_db = -100.0}}, .taps_declared = true});
    config.models.emplace(model.id, model);
    if (noise) {
      ocg::ModelConfig floor;
      floor.id = "floor";
      floor.chain.push_back({.type = ocg::ModelStepType::Awgn, .params = {{"noise_power", 1.0e-2}}});
      config.models.emplace(floor.id, floor);
    }
    config.links = {{.from = "tx", .to = "rx", .model = model.id},
                    {.from = "rx", .to = "tx", .model = model.id}};
    require(ocg::validate_config(config).empty(), "fixture must validate");
    processor = ocg::create_channel_processor(config);
    for (const auto& lane : ocg::resolve_topology(config).lanes) {
      if (lane.dst_node != "rx") continue;
      lanes.push_back({.link_key = lane.key, .model = ocg::find_model(config, lane.model_id),
                       .samples = {}, .rx_port = lane.rx_port, .tx_port = lane.tx_port});
    }
    if (noise) rx_model = ocg::find_model(config, "floor");
    control = processor->collect_control_links().at("tx>rx:dynamic");
  }

  // One call of `count` samples (zero input), returning the RX row.
  ocg::IqBuffer run(std::size_t count)
  {
    ocg::IqBuffer in(count);
    for (auto& lane : lanes) lane.samples = std::span<const ocg::IqSample>(in.data(), count);
    ocg::IqBuffer out(count);
    std::span<ocg::IqSample> rows[1] = {std::span<ocg::IqSample>(out.data(), count)};
    processor->process_superposition("rx", lanes, rx_model, 23040000, std::span<std::span<ocg::IqSample>>(rows));
    return out;
  }
};

void control_messages()
{
  auto ctl = std::make_unique<ocg::BrokerLinkControl>();
  ctl->nt_hint = 1;
  ctl->nr_hint = 1;
  ctl->current_sample.store(100);
  ocg::ControlServer::LinkMap map = {{"tx>rx:dynamic", ctl.get()}};
  std::vector<std::string> log;
  ocg::ControlServerConfig cfg;
  cfg.endpoint = "inproc://test-sample-indexed";
  cfg.logger = [&log](std::string_view line) { log.emplace_back(line); };
  ocg::ControlServer server(std::move(cfg), std::move(map));

  require(contains(server.handle_message(R"({"type":"batch_begin","id":"B"})"), "\"ok\":true"), "begin");
  require(contains(server.handle_message(kMatrix), "\"staged\":true"), "matrix stages");
  const std::string reply = server.handle_message(
      R"({"type":"batch_commit","id":"B","take_effect_at_sample":46080,"tag":"k=000001;sha=ab12","noise_epoch":true})");
  require(contains(reply, "\"ok\":true"), "tagged commit accepted: " + reply);
  require(contains(reply, "\"apply_at_sample\":46080"), "reply echoes the target sample");
  require(contains(reply, "\"late_links\":0"), "target in the future is not late");
  require(ctl->take_effect_at_sample.load() == 46080, "target sample stored");
  require(ctl->noise_epoch_at_sample.load() == 46080, "noise epoch stored");
  require(std::string(ctl->pending_tag) == "k=000001;sha=ab12", "tag stored");
  require(ctl->pending_digest == ocg::matrix_profile_digest(ctl->shadow_matrix_profile) && ctl->pending_digest != 0,
          "digest is the decoded profile's");
  require(contains(log.back(), "apply_at_sample=46080 tag=k=000001;sha=ab12 noise_epoch=1 late_links=0"),
          "commit log names the target: " + log.back());

  // The digest follows the decoded values: one changed tap gain changes it.
  ocg::MatrixProfileShadow other = ctl->shadow_matrix_profile;
  other.lanes[0].taps[0].gain_db += 1e-9;
  require(ocg::matrix_profile_digest(other) != ctl->pending_digest, "digest sees a 1e-9 dB change");

  // A target already passed is accepted (the evidence is the apply log) but counted late.
  server.handle_message(R"({"type":"batch_begin","id":"L"})");
  server.handle_message(R"({"type":"matrix_profile_swap","link_id":"tx>rx:dynamic","batch_id":"L","nt":1,"nr":1,)"
                        R"("lanes":[{"rx_port":0,"tx_port":0,"taps":[{"delay_samples":0,"gain_db":0}]}]})");
  require(contains(server.handle_message(R"({"type":"batch_commit","id":"L","take_effect_at_sample":50,"tag":"late"})"),
                   "\"late_links\":1"), "past target counted late");

  for (const char* bad : {
           R"({"type":"batch_commit","id":"X","take_effect_at_sample":10,"tag":"has space"})",
           R"({"type":"batch_commit","id":"X","take_effect_at_sample":-1})",
           R"({"type":"batch_commit","id":"X","take_effect_at_sample":1.5})",
           R"({"type":"batch_commit","id":"X","noise_epoch":true})"}) {
    server.handle_message(R"({"type":"batch_begin","id":"X"})");
    require(contains(server.handle_message(bad), "\"ok\":false"), std::string("must reject: ") + bad);
    server.handle_message(R"({"type":"batch_abort","id":"X"})");
  }

  // Untagged commits keep the legacy log line and reply.
  server.handle_message(R"({"type":"batch_begin","id":"U"})");
  const std::string legacy = server.handle_message(R"({"type":"batch_commit","id":"U"})");
  require(!contains(legacy, "apply_at_sample"), "untagged reply unchanged");
  require(!contains(log.back(), "apply_at_sample"), "untagged log unchanged");

  const std::string clock = server.handle_message(R"({"type":"link_clock"})");
  require(contains(clock, "{\"link_id\":\"tx>rx:dynamic\",\"sample\":100,"), "link_clock reports the sample: " + clock);
  require(contains(clock, "\"applied_seqno\":0"), "nothing applied yet");
}

void gate_lands_on_target(ocg::Backend backend, const char* label)
{
  Fixture f(backend, false);
  ocg::ControlServer::LinkMap map = {{"tx>rx:dynamic", f.control}};
  ocg::ControlServerConfig cfg;
  cfg.endpoint = std::string("inproc://test-gate-") + label;
  cfg.logger = [](std::string_view) {};
  ocg::ControlServer server(std::move(cfg), std::move(map));
  server.handle_message(R"({"type":"batch_begin","id":"B"})");
  require(contains(server.handle_message(kMatrix), "\"staged\":true"), "stage");
  require(contains(server.handle_message(R"({"type":"batch_commit","id":"B","take_effect_at_sample":24,"tag":"k=0"})"),
                   "\"ok\":true"), "commit");
  const std::uint32_t seqno = f.control->seqno.load();
  // Slots start at 0, 8, 16: still gated. The slot starting at 24 takes it.
  for (int call = 0; call < 3; ++call) {
    f.run(8);
    require(f.control->applied_seqno.load() != seqno, std::string(label) + ": applied before its target sample");
  }
  f.run(8);
  require(f.control->applied_seqno.load() == seqno, std::string(label) + ": not applied at its target sample");
  require(f.control->current_sample.load() == 24, std::string(label) + ": link clock is the slot start sample");
}

void noise_epoch_is_a_fresh_stream()
{
  // CPU reference: honestly unsupported.
  {
    Fixture cpu(ocg::Backend::Cpu, true);
    require(!cpu.processor->reset_receiver_noise("rx"), "CPU backend must not claim a counter reset");
  }
#if OCUDU_GPU_CHANNEL_HAS_CUDA
  // Fresh processor: the first 16 samples of noise.
  Fixture fresh(ocg::Backend::Cuda, true);
  ocg::IqBuffer reference = fresh.run(8);
  const auto second = fresh.run(8);
  reference.insert(reference.end(), second.begin(), second.end());
  double energy = 0.0;
  for (const auto& s : reference) energy += ocg::power(s);
  require(energy > 0.0, "noise floor must produce noise");

  // Advanced by an odd amount, then reset, then read in different fragments.
  Fixture reused(ocg::Backend::Cuda, true);
  reused.run(5);
  reused.run(8);
  reused.run(3);
  require(reused.processor->reset_receiver_noise("rx"), "CUDA backend resets its receiver counters");
  ocg::IqBuffer after;
  for (std::size_t count : {3u, 8u, 5u}) {
    const auto part = reused.run(count);
    after.insert(after.end(), part.begin(), part.end());
  }
  require(after.size() == reference.size(), "same sample count");
  for (std::size_t i = 0; i != after.size(); ++i) {
    require(after[i].i == reference[i].i && after[i].q == reference[i].q,
            "noise after the epoch must equal a fresh stream bit for bit (sample " + std::to_string(i) + ")");
  }
  require(!reused.processor->reset_receiver_noise("nope"), "unknown destination is not a reset");
#endif
}

}  // namespace

int main()
{
  control_messages();
  gate_lands_on_target(ocg::Backend::Cpu, "cpu");
#if OCUDU_GPU_CHANNEL_HAS_CUDA
  gate_lands_on_target(ocg::Backend::Cuda, "cuda");
#endif
  noise_epoch_is_a_fresh_stream();
  std::cout << "test_sample_indexed_control: pass\n";
  return 0;
}
