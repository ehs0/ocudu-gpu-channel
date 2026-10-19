"""Shared definitions of the scheduler benchmark.

One module owns the vocabulary every other part reads -- the gate renderer,
the traffic generator, the analyzer and the web UI -- so a scheduler, a 5QI
or a traffic profile cannot mean one thing in the config and another in the
report.

Source references are to the pinned OCUDU tree
(`ocudu-spark/src/ocudu`, commit a1916edcdbcd70ba6e0af47ee87be061dad5a4e4).
"""

from __future__ import annotations

import copy
from typing import Any

# --- Cells -------------------------------------------------------------------
# Cell a's radios are the ones the Sionna scenario names; cell b's are the
# fan-out copies (run_bridge.py --fanout-control-endpoint with this rename),
# so ue2 sees exactly ue0's solved channel and ue3 exactly ue1's.
CELLS = (
    {
        "name": "a",
        "gnb": {"device_id": "gnb0", "tx_port": 2000, "rx_port": 2001, "pci": 1,
                "gnb_id": 411, "bind": "127.0.0.11", "metrics_port": 8001},
        "ues": ("ue0", "ue1"),
    },
    {
        "name": "b",
        "gnb": {"device_id": "gnb1", "tx_port": 2010, "rx_port": 2011, "pci": 2,
                "gnb_id": 412, "bind": "127.0.0.12", "metrics_port": 8002},
        "ues": ("ue2", "ue3"),
    },
)
FANOUT_RENAME = {"gnb0": "gnb1", "ue0": "ue2", "ue1": "ue3"}
# Twin of each UE in the other cell: same channel, same traffic schedule.
TWINS = {"ue0": "ue2", "ue1": "ue3", "ue2": "ue0", "ue3": "ue1"}
UE_GATEWAY = "10.45.1.1"

# --- Schedulers ----------------------------------------------------------------
# The two intra-slice policies the pinned OCUDU offers
# (lib/scheduler/policy/scheduler_policy_factory.cpp:13-19), and Max C/I, a
# parameterisation of the QoS policy. All are chosen at gNB start-up; there is
# no runtime switch (the remote-control commands do not include one), which is
# why the benchmark runs them side by side. `policy` is the YAML policy block
# and `parameters` its values: the gate identifies the loaded policy from the
# gNB's config dump by both (schedulers_in_config_dump).
SCHEDULERS: dict[str, dict[str, Any]] = {
    "rr": {
        "title": "Round-Robin (time domain)",
        "short": "RR",
        "source": "lib/scheduler/policy/scheduler_time_rr.cpp",
        "yaml": "  scheduler:\n    policy:\n      rr_sched:\n",
        "policy": "rr_sched",
        "config_dump_token": "rr_sched:",
        "summary": (
            "Serves the UEs in turn. A UE's priority is the number of newTx "
            "allocations made since it was last allocated, so the UE waiting "
            "longest goes first."
        ),
        "properties": [
            "Channel-blind: ignores CQI, MCS and the achievable rate.",
            "QoS-blind: ignores 5QI priority, packet delay budget and GBR.",
            "Turns are counted in allocation slots, separately for DL and UL.",
            "Expected: equal access per UE; a UE in a poor channel gets the "
            "same turns as a good one, so aggregate throughput can drop.",
        ],
        "parameters": {},
    },
    "qos": {
        "title": "QoS-aware Proportional Fair (time domain)",
        "short": "QoS-PF",
        "source": "lib/scheduler/policy/scheduler_time_qos.cpp",
        # Written out explicitly although every value equals the default
        # (scheduler_expert_config.h:26-52): the gNB's non-default config dump
        # then shows `qos_sched`, which is how the gate proves the policy.
        "yaml": (
            "  scheduler:\n"
            "    policy:\n"
            "      qos_sched:\n"
            "        combine_function: gbr_prioritized\n"
            "        pf_fairness_coeff: 2.0\n"
            "        prio_enabled: true\n"
            "        pdb_enabled: true\n"
            "        gbr_enabled: true\n"
        ),
        "policy": "qos_sched",
        "config_dump_token": "qos_sched:",
        "summary": (
            "Ranks UEs by weight = GBR x PF x priority x delay and serves the "
            "largest first. This is OCUDU's default policy."
        ),
        "properties": [
            "PF = r_est / r_avg^a, a = pf_fairness_coeff = 2.0; r_est is the "
            "TBS the UE's current MCS would carry over the full bandwidth, "
            "r_avg an exponential average of its past TBS (alpha 0.01).",
            "Priority = (max + 1 - min(5QI priority x ARP priority)) / (max + 1) "
            "over the UE's logical channels with data.",
            "Delay = sum of head-of-line delay / PDB over those channels "
            "(1 when no data is waiting).",
            "GBR = sum of GBR / average rate over GBR flows (1 when none).",
            "A UE that has never been allocated goes first.",
        ],
        "parameters": {
            "combine_function": "gbr_prioritized", "pf_fairness_coeff": 2.0,
            "prio_enabled": True, "pdb_enabled": True, "gbr_enabled": True,
        },
    },
    "maxci": {
        "title": "Max C/I (maximum rate)",
        "short": "Max C/I",
        "source": "lib/scheduler/policy/scheduler_time_qos.cpp (pf_fairness_coeff 0, QoS terms off)",
        # The QoS policy with every fairness and QoS term removed: with a = 0
        # the PF weight r_est / r_avg^a is r_est, and the priority, delay and
        # GBR weights are 1 when disabled (compute_dl_qos_weights), so the
        # weight is the UE's estimated rate alone.
        "yaml": (
            "  scheduler:\n"
            "    policy:\n"
            "      qos_sched:\n"
            "        combine_function: gbr_prioritized\n"
            "        pf_fairness_coeff: 0.0\n"
            "        prio_enabled: false\n"
            "        pdb_enabled: false\n"
            "        gbr_enabled: false\n"
        ),
        "policy": "qos_sched",
        # Only for the substring check of gates that predate
        # schedulers_in_config_dump; it never occurs in the QoS-PF block.
        "config_dump_token": "pf_fairness_coeff: 0",
        "summary": (
            "Serves the UE whose current channel supports the highest rate first "
            "(weight = estimated rate). Maximises cell throughput at the cost of "
            "fairness: UEs in a poor channel wait."
        ),
        "properties": [
            "OCUDU's QoS policy with pf_fairness_coeff = 0 and the priority, "
            "PDB and GBR terms disabled, so the weight is r_est alone.",
            "Channel-aware, fairness- and QoS-blind; no average-rate memory.",
            "Not a complete starvation: a UE never allocated goes first once, "
            "and PRBs the first UE does not need go to the next.",
            "Expected: highest cell throughput, lowest Jain index, long waits "
            "for the weakest UE when the cell is oversubscribed.",
        ],
        "parameters": {
            "combine_function": "gbr_prioritized", "pf_fairness_coeff": 0.0,
            "prio_enabled": False, "pdb_enabled": False, "gbr_enabled": False,
        },
    },
}


def _dump_value(text: str) -> Any:
    text = text.strip()
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    try:
        return float(text)
    except ValueError:
        return text


def schedulers_in_config_dump(policy_block: str) -> list[str]:
    """The schedulers whose policy and parameters equal a gNB config-dump
    policy block (the lines under `policy:`, e.g. "  qos_sched:\n    pf_...").

    Values are compared as numbers and booleans, so 0 and 0.0 match; a
    parameter the dump leaves out takes the QoS policy's default
    (scheduler_expert_config.h), which is what the gNB runs with."""

    lines = [line for line in policy_block.splitlines() if line.strip()]
    if not lines:
        return []
    policy = lines[0].strip().split(":", 1)[0]
    found = {}
    for line in lines[1:]:
        key, _, value = line.strip().partition(":")
        found[key.strip()] = _dump_value(value)
    defaults = SCHEDULERS["qos"]["parameters"] if policy == "qos_sched" else {}
    loaded = {**defaults, **found}
    return [name for name, info in SCHEDULERS.items()
            if info["policy"] == policy and {k: _dump_value(str(v)) for k, v in info["parameters"].items()} == loaded]

# --- 5QI characteristics ---------------------------------------------------------
# lib/ran/qos/five_qi_qos_mapping.cpp (TS 23.501 Table 5.7.4-1) and the DU's
# default QoS list include/ocudu/du/du_high/du_qos_config_helpers.h (RLC mode).
FIVE_QI = {
    5: {"type": "non-GBR", "priority": 10, "pdb_ms": 100, "rlc": "AM", "example": "IMS signalling"},
    6: {"type": "non-GBR", "priority": 60, "pdb_ms": 300, "rlc": "AM", "example": "buffered video, TCP"},
    7: {"type": "non-GBR", "priority": 70, "pdb_ms": 100, "rlc": "UM", "example": "voice, live video, gaming"},
    8: {"type": "non-GBR", "priority": 80, "pdb_ms": 300, "rlc": "AM", "example": "buffered video, TCP"},
    9: {"type": "non-GBR", "priority": 90, "pdb_ms": 300, "rlc": "AM", "example": "buffered video, TCP (default)"},
}
# Open5GS subscribers get ARP priority level 8 (docker/open5gs/add_users.py).
ARP_PRIORITY = 8

# --- Traffic profiles ------------------------------------------------------------
# Per cell-local UE index. Every cell runs the same profile, and twins get the
# same seeded packet schedule. Rates are offered load at the UDP layer.
#
# Flow patterns:
#   cbr      fixed size, fixed interval from rate
#   periodic fixed size every interval_ms
#   poisson  fixed size, exponential inter-arrivals with mean from rate
#   onoff    cbr at rate during exponential ON periods, silent during OFF
TRAFFIC_PROFILES: dict[str, dict[str, Any]] = {
    "mixed": {
        "title": "Bulk (5QI 9) + delay-sensitive (5QI 7)",
        "description": (
            "ue A is a backlogged download (5QI 9, PDB 300 ms); ue B carries "
            "on/off video and a 100 Hz control stream (5QI 7, PDB 100 ms). "
            "The cell is oversubscribed, so the policies must choose."
        ),
        "ues": [
            {"role": "bulk", "five_qi": 9, "flows": [
                {"name": "dl_bulk", "direction": "dl", "pattern": "cbr", "rate_mbps": 40.0, "size_bytes": 1200},
                {"name": "ul_bulk", "direction": "ul", "pattern": "cbr", "rate_mbps": 2.0, "size_bytes": 1200},
            ]},
            {"role": "interactive", "five_qi": 7, "flows": [
                {"name": "dl_video", "direction": "dl", "pattern": "onoff", "rate_mbps": 12.0, "size_bytes": 1200,
                 "on_mean_s": 2.0, "off_mean_s": 1.0},
                {"name": "dl_control", "direction": "dl", "pattern": "periodic", "interval_ms": 10.0,
                 "size_bytes": 200},
                {"name": "ul_control", "direction": "ul", "pattern": "periodic", "interval_ms": 10.0,
                 "size_bytes": 200},
            ]},
        ],
    },
    "full_buffer": {
        "title": "Full buffer, equal QoS (5QI 9)",
        "description": (
            "Both UEs download a backlogged stream with the same 5QI, so the "
            "QoS-aware policy's priority term is equal and what remains is PF "
            "against round robin."
        ),
        "ues": [
            {"role": "bulk", "five_qi": 9, "flows": [
                {"name": "dl_bulk", "direction": "dl", "pattern": "cbr", "rate_mbps": 40.0, "size_bytes": 1200},
                {"name": "ul_bulk", "direction": "ul", "pattern": "cbr", "rate_mbps": 2.0, "size_bytes": 1200},
            ]},
            {"role": "bulk", "five_qi": 9, "flows": [
                {"name": "dl_bulk", "direction": "dl", "pattern": "cbr", "rate_mbps": 40.0, "size_bytes": 1200},
                {"name": "ul_bulk", "direction": "ul", "pattern": "cbr", "rate_mbps": 2.0, "size_bytes": 1200},
            ]},
        ],
    },
    "light": {
        "title": "Light load (both 5QI 9)",
        "description": "Poisson traffic well below capacity: a control where both policies should look alike.",
        "ues": [
            {"role": "light", "five_qi": 9, "flows": [
                {"name": "dl_poisson", "direction": "dl", "pattern": "poisson", "rate_mbps": 4.0, "size_bytes": 1000},
                {"name": "ul_poisson", "direction": "ul", "pattern": "poisson", "rate_mbps": 0.5, "size_bytes": 500},
            ]},
            {"role": "light", "five_qi": 9, "flows": [
                {"name": "dl_poisson", "direction": "dl", "pattern": "poisson", "rate_mbps": 4.0, "size_bytes": 1000},
                {"name": "ul_poisson", "direction": "ul", "pattern": "poisson", "rate_mbps": 0.5, "size_bytes": 500},
            ]},
        ],
    },
}
FLOW_PATTERNS = ("cbr", "periodic", "poisson", "onoff")
# UDP ports. DL flow f of cell-local UE u is received in the UE's namespace on
# DL_BASE_PORT + 10 * u + f; every UL flow arrives at one root-namespace port.
DL_BASE_PORT = 47000
UL_PORT = 47900


def flow_id(cell_index: int, ue_index: int, flow_index: int) -> int:
    """16-bit flow id carried in every packet: cell, cell-local UE, flow."""

    if not (0 <= cell_index < 16 and 0 <= ue_index < 16 and 0 <= flow_index < 256):
        raise ValueError("flow id field out of range")
    return (cell_index << 12) | (ue_index << 8) | flow_index


def split_flow_id(value: int) -> tuple[int, int, int]:
    return (value >> 12) & 0xF, (value >> 8) & 0xF, value & 0xFF


def resolve_profile(name: str, overrides: dict[str, float] | None = None) -> dict[str, Any]:
    """A copy of a profile, with `ueI.flow.key=value` overrides applied and checked."""

    if name not in TRAFFIC_PROFILES:
        raise ValueError(f"unknown traffic profile {name!r}; known: {', '.join(sorted(TRAFFIC_PROFILES))}")
    profile = copy.deepcopy(TRAFFIC_PROFILES[name])
    profile["name"] = name
    for key, value in (overrides or {}).items():
        parts = key.split(".")
        if len(parts) != 3 or not parts[0].startswith("ue") or not parts[0][2:].isdigit():
            raise ValueError(f"override {key!r} must be ue<i>.<flow>.<field>")
        index = int(parts[0][2:])
        if index >= len(profile["ues"]):
            raise ValueError(f"override {key!r}: profile has {len(profile['ues'])} UEs")
        if parts[1] == "five_qi" and parts[2] == "value":
            profile["ues"][index]["five_qi"] = int(value)
            continue
        flows = {flow["name"]: flow for flow in profile["ues"][index]["flows"]}
        if parts[1] not in flows:
            raise ValueError(f"override {key!r}: no flow {parts[1]!r}")
        if parts[2] not in ("rate_mbps", "size_bytes", "interval_ms", "on_mean_s", "off_mean_s"):
            raise ValueError(f"override {key!r}: field {parts[2]!r} cannot be overridden")
        flows[parts[1]][parts[2]] = float(value) if parts[2] != "size_bytes" else int(value)
    validate_profile(profile)
    return profile


def validate_profile(profile: dict[str, Any], ue_count: int = 2) -> None:
    ues = profile.get("ues")
    if not isinstance(ues, list) or len(ues) != ue_count:
        if ue_count == 2:
            raise ValueError("a traffic profile defines exactly two UEs per cell")
        raise ValueError(f"a traffic profile for {ue_count} UEs defines {ue_count} UE entries")
    for index, ue in enumerate(ues):
        if ue.get("five_qi") not in FIVE_QI:
            raise ValueError(f"ue{index}: 5QI {ue.get('five_qi')} is not in the supported table {sorted(FIVE_QI)}")
        names = set()
        for flow in ue.get("flows") or []:
            if flow["name"] in names:
                raise ValueError(f"ue{index}: duplicate flow {flow['name']}")
            names.add(flow["name"])
            if flow.get("direction") not in ("dl", "ul"):
                raise ValueError(f"ue{index}.{flow['name']}: direction must be dl or ul")
            if flow.get("pattern") not in FLOW_PATTERNS:
                raise ValueError(f"ue{index}.{flow['name']}: pattern must be one of {FLOW_PATTERNS}")
            size = flow.get("size_bytes")
            if not isinstance(size, int) or not 64 <= size <= 1400:
                raise ValueError(f"ue{index}.{flow['name']}: size_bytes must be 64..1400")
            if flow["pattern"] == "periodic":
                if not flow.get("interval_ms", 0) > 0:
                    raise ValueError(f"ue{index}.{flow['name']}: periodic needs interval_ms > 0")
            elif not flow.get("rate_mbps", 0) > 0:
                raise ValueError(f"ue{index}.{flow['name']}: needs rate_mbps > 0")
            if flow["pattern"] == "onoff" and not (flow.get("on_mean_s", 0) > 0 and flow.get("off_mean_s", 0) > 0):
                raise ValueError(f"ue{index}.{flow['name']}: onoff needs on_mean_s and off_mean_s > 0")
        if len(ue["flows"]) > 10:
            raise ValueError(f"ue{index}: at most 10 flows (DL port block of 10)")


def offered_rate_mbps(flow: dict[str, Any]) -> float:
    """Mean offered UDP payload rate of one flow."""

    if flow["pattern"] == "periodic":
        return flow["size_bytes"] * 8 / (flow["interval_ms"] / 1000.0) / 1e6
    if flow["pattern"] == "onoff":
        duty = flow["on_mean_s"] / (flow["on_mean_s"] + flow["off_mean_s"])
        return flow["rate_mbps"] * duty
    return float(flow["rate_mbps"])


# --- Single-cell mode ------------------------------------------------------------
# One gNB serves N UEs, and the policies run one after another on the same
# cell (single_cell_runner.py). Nothing below depends on CELLS: the topology
# is generated from the UE count, and the policy vocabulary above is shared.
SINGLE_CELL_GNB = {"device_id": "gnb0", "tx_port": 2000, "rx_port": 2001, "metrics_port": 8001}
# gNB antenna configurations. Port i of the gNB uses ZMQ tx 2000 + 2i / rx
# 2001 + 2i (the rank-1 fixtures' layout); the UEs stay one-port srsUEs, so
# 2 antennas means DL 2x1 MISO and UL 1x2 SIMO, one layer. The Sionna array
# is a 1 x N half-wavelength ULA, and the channel of every (rx, tx) antenna
# pair is solved and replayed as its own lane.
GNB_ANTENNA_CONFIGS: dict[int, dict[str, Any]] = {
    1: {"label": "1T1R (SISO)", "short": "1T1R"},
    2: {"label": "2T2R (DL 2x1 MISO, UL 1x2 SIMO)", "short": "2T2R"},
}


def single_cell_gnb_ports(antennas: int) -> list[dict[str, int]]:
    """ZMQ endpoints of gNB antenna ports 0..antennas-1."""

    if antennas not in GNB_ANTENNA_CONFIGS:
        raise ValueError(f"gNB antennas must be one of {sorted(GNB_ANTENNA_CONFIGS)}")
    return [{"tx_port": SINGLE_CELL_GNB["tx_port"] + 2 * i, "rx_port": SINGLE_CELL_GNB["rx_port"] + 2 * i}
            for i in range(antennas)]


# Records exist for this many UEs (flow ids carry the UE index in 4 bits);
# which counts may RUN is decided by scene_registry.py from validation
# evidence, not by this table.
MAX_SINGLE_CELL_UES = 16
# Offered DL load of `full_buffer`, split equally over the UEs, so the cell is
# oversubscribed (a 20 MHz SISO carrier tops out near 70 Mb/s of radio time)
# at every UE count without the per-UE share depending on anything but N.
FULL_BUFFER_TOTAL_DL_MBPS = 80.0
# `mixed`: the bulk role's DL load, split over the bulk UEs.
MIXED_TOTAL_BULK_DL_MBPS = 40.0


def single_cell_prach_preamble(index: int) -> int:
    """The contention preamble UE `index` pins (srsue-ra-contention.patch takes it
    modulo 64): a stride of 4 keeps all 16 UEs on distinct preambles."""

    return 4 * index


def single_cell_ue(index: int) -> dict[str, Any]:
    """Identity, ZMQ ports and namespace of UE `index` (ue0..ue15).

    The first four equal render-multi-ue-configs.py's UES table, so a
    single-cell run reuses the identities the multi-UE gates proved.
    """

    if not 0 <= index < MAX_SINGLE_CELL_UES:
        raise ValueError(f"UE index {index} outside 0..{MAX_SINGLE_CELL_UES - 1}")
    return {
        "device_id": f"ue{index}",
        "tx_port": 2101 + 2 * index,
        "rx_port": 2100 + 2 * index,
        "imsi": f"0010101234567{80 + index:02d}",
        "imei": f"3534900698733{19 + index:02d}",
        "netns": f"ue{index + 1}",
        "ipv4": f"10.45.1.{2 + index}",
    }


SINGLE_CELL_PROFILES = {
    "full_buffer": {
        "title": "Full buffer, equal QoS (5QI 9)",
        "role_rule": "every UE is bulk: a backlogged DL stream of "
                     f"{FULL_BUFFER_TOTAL_DL_MBPS:g}/N Mb/s and a 2 Mb/s UL stream, 5QI 9",
    },
    "mixed": {
        "title": "Bulk (5QI 9) + delay-sensitive (5QI 7)",
        "role_rule": "even UE index = bulk (DL "
                     f"{MIXED_TOTAL_BULK_DL_MBPS:g}/n_bulk Mb/s + 2 Mb/s UL, 5QI 9); odd index = "
                     "interactive (on/off 12 Mb/s video, 100 Hz DL/UL control, 5QI 7)",
    },
}


def single_cell_profile(name: str, ue_count: int) -> dict[str, Any]:
    """The traffic profile for N UEs, with every UE's role written out.

    Roles follow a fixed rule of the UE index, so ue0 has the same role and
    flows at every N (only the bulk share scales with the number of bulk UEs)
    and no UE is left without traffic.
    """

    if name not in SINGLE_CELL_PROFILES:
        raise ValueError(f"unknown single-cell traffic profile {name!r}; known: {', '.join(SINGLE_CELL_PROFILES)}")
    if not 1 <= ue_count <= MAX_SINGLE_CELL_UES:
        raise ValueError(f"UE count must be 1..{MAX_SINGLE_CELL_UES}")
    bulk, interactive = TRAFFIC_PROFILES["mixed"]["ues"]
    ues = []
    if name == "full_buffer":
        rate = round(FULL_BUFFER_TOTAL_DL_MBPS / ue_count, 6)
        for _ in range(ue_count):
            ue = copy.deepcopy(bulk)
            ue["flows"][0]["rate_mbps"] = rate
            ues.append(ue)
    else:
        n_bulk = (ue_count + 1) // 2
        rate = round(MIXED_TOTAL_BULK_DL_MBPS / n_bulk, 6)
        for index in range(ue_count):
            ue = copy.deepcopy(bulk if index % 2 == 0 else interactive)
            if index % 2 == 0:
                ue["flows"][0]["rate_mbps"] = rate
            ues.append(ue)
    profile = {"name": name, "ue_count": ue_count, **SINGLE_CELL_PROFILES[name], "ues": ues}
    profile["description"] = profile["role_rule"]
    validate_profile(profile, ue_count)
    profile["offered_mbps"] = {
        direction: round(sum(offered_rate_mbps(f) for ue in ues for f in ue["flows"] if f["direction"] == direction), 6)
        for direction in ("dl", "ul")
    }
    return profile


# --- Comparison tests ------------------------------------------------------------
# A session compares two tests. Each test is the shared configuration plus the
# fields below; today a test differs only in its MAC scheduler. Another
# comparison dimension (traffic profile, a gNB knob, ...) is added by naming
# its field and choices here and applying it where the policy run is built
# (single_cell_runner.Session.run_test). Both tests may pick the same value:
# that session is an A/A control.
TEST_ARMS = ("test1", "test2")
TEST_ARM_LABELS = {"test1": "Test 1", "test2": "Test 2"}
TEST_FIELDS: dict[str, tuple[str, ...]] = {"scheduler": tuple(SCHEDULERS)}


def test_label(arm: str, test: dict[str, Any]) -> str:
    """'Test 1 (RR)': the arm and what distinguishes it."""

    parts = [SCHEDULERS[test["scheduler"]]["short"]] if "scheduler" in test else []
    parts += [f"{key}={value}" for key, value in sorted(test.items()) if key != "scheduler"]
    return f"{TEST_ARM_LABELS[arm]} ({', '.join(parts)})" if parts else TEST_ARM_LABELS[arm]
