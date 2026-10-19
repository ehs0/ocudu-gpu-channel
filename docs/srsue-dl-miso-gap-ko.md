# srsUE 구성에서 DL MISO 이득이 측정되지 않은 이유

관련 문서: [`rank1-feasibility-report.md`](rank1-feasibility-report.md) · [`why-radionode-and-srsue-rank1-ko.md`](why-radionode-and-srsue-rank1-ko.md) · [`examples/scheduler_benchmark/README.md`](../examples/scheduler_benchmark/README.md) "gNB antennas"

## 결론

이 repo에서 srsUE를 쓰는 2×1 / 4×1 구성은 **어느 것도 DL MISO 이득을 실측한 적이 없다.**
gNB를 2T(또는 4T)로 설정해도, srsUE와 붙이려면 CSI-RS를 꺼야 한다. CSI-RS가 꺼지면 OCUDU는 SSB, PDCCH, PDSCH를 전부 port 0으로만 내보낸다. UE가 받는 신호는 `y = h0·x + n`이고, 1T 셀과 구별되지 않는다.

지금까지 측정된 것은 다음 세 가지이며, 모두 "OCUDU가 여러 port로 송신해서 UE의 링크 품질이 좋아졌다"는 측정이 아니다.

- 에뮬레이터가 다중 branch DL 채널을 정확히 계산한다 (합성 peer, CSI-on 캡처)
- 프리코더 `w`가 주어지면 유효 채널 `h·w`가 예측대로 나온다 (oracle 실험; 이때도 gNB는 port 0만 송신)
- UL 1×2 / 1×4 SIMO 수신 다이버시티는 실제로 동작한다

## 1. 무엇을 "MISO 이득 측정"으로 보는가

이 문서에서 DL MISO 이득을 측정했다고 말하려면 같은 실행 안에서 세 조건이 모두 성립해야 한다.

1. gNB 스택이 **두 개 이상의 TX port에 0이 아닌 weight**로 같은 layer를 송신한다.
2. UE가 그 합성 신호 `y = (h0·w0 + h1·w1)·x + n`으로 attach하고 데이터를 복호한다.
3. 같은 채널·같은 UE 위치에서 port 0 단독 송신 대비 SNR, BLER, MCS 또는 처리량 차이를 비교한다.

조건 1이 가장 먼저 깨진다. 나머지 두 조건은 조건 1 없이는 의미가 없다.

## 2. 원인: CSI-RS를 끄면 port 0만 남는다

OCUDU 기준은 repo가 고정한 커밋 `a1916edc`이다 (경로는 OCUDU 소스 트리 기준).

| 단계 | 동작 | 근거 |
|---|---|---|
| 1 | DL 안테나가 2개면 OCUDU는 NZP-CSI-RS를 `nof_antennas_dl` 크기, 즉 2-port(FD-CDM2)로 만든다. | `lib/scheduler/config/builder/csi_meas_config.cpp:21` (`csi_params.nof_ports = cell_cfg.dl_carrier.nof_ant`) |
| 2 | srsUE NR(PoC)은 2-port CSI-RS 리소스 매핑을 구현하지 않았다. `Resource mapping is invalid or not implemented`가 나고 RRC connection setup 전체가 실패한다. | [`rank1-feasibility-report.md`](rank1-feasibility-report.md) §6 제약 #2 |
| 3 | 접속을 살리려고 모든 srsUE 2T/4T 구성이 `csi_rs_enabled: false`, `nof_cell_csi_res: 0`을 쓴다. | `examples/configs/ran/ocudu/*/gnb_*_rank1*_srsue.yaml`, `render_single_cell.GNB_2T2R_CHANGES` |
| 4 | scheduler는 UE의 DL port 수를 CSI-RS 리소스의 port 수로 계산한다. CSI 측정 설정이 없으면 1이다. 셀은 2T인데 UE는 1-port로 취급된다. | `lib/scheduler/config/ue_configuration.cpp:364`, `:632` |
| 5 | 1-port UE의 PDSCH에는 프리코딩 정보를 붙이지 않는다. | `lib/scheduler/ue_context/ue_channel_state_manager.h:56` |
| 6 | FAPI 변환에서 프리코딩 정보가 없는 PDSCH는 "omnidirectional" 행렬을 받는다. 이 행렬은 `make_one_layer_one_port(N, 0)`, 즉 `w = [1, 0, …]ᵀ`이다. | `lib/fapi_adaptor/precoding_matrix_mapper.cpp:95`, `lib/fapi_adaptor/precoding_matrix_table_generator.cpp` (`generate_pdsch_omnidirectional`) |
| 7 | SSB와 PDCCH는 CSI 설정과 무관하게 항상 `make_one_layer_one_port(N, 0)`을 쓴다. | 같은 파일의 `generate_ssb`, `generate_pdcch` |

결과적으로 port 1 이상의 IQ 스트림은 정확히 0이다. rank-1 작업에서 와이어 캡처로 이미 실측했다 ([`rank1-feasibility-report.md`](rank1-feasibility-report.md) §6 "정직한 잔여 한계").

OCUDU에 다른 송신 방식이 있었다면 우회할 수 있었겠지만, 없다.

- NR 규격에는 LTE TM2 같은 송신 다이버시티 모드가 없다. OCUDU에도 Alamouti(SFBC/STBC), CDD, precoder cycling 구현이 없다.
- 다중 port를 쓰는 PDSCH 프리코딩은 TS 38.214 코드북(2-port 표, Type I Single-Panel) 뿐이고, 모두 UE의 PMI 보고를 전제로 한다.
- 모든 port에 같은 신호를 싣는 `make_one_layer_all_ports`는 테스트·예제 전용이다. 이 방식은 다이버시티도 아니다. 아래 oracle 실험에서 port 0 단독보다 오히려 나빴다.

## 3. repo 구성별 상태

| 구성 | UE | CSI-RS | DL에서 실제로 송신하는 port | DL MISO 이득 측정 |
|---|---|---|---|---|
| `gnb_zmq_b210_fdd_2t2r_rank1_srsue.yaml` (native, docker), `…_2t2r_rank1_multi_ue_srsue.yaml` + `topology.ocudu*.rank1-2x1*.cuda.yaml` (1/2/3/4 UE) | srsUE | off | port 0 | 없음 |
| `gnb_zmq_b210_fdd_4t4r_rank1_srsue.yaml`, `…_4t2r_rank1_srsue.yaml` + `rank1-4x1*` | srsUE | off | port 0 (1–3은 0) | 없음 |
| `topology.ocudu.rank1-2x1-oracle-mrt.cuda.yaml` | srsUE | off | port 0 | 없음. 에뮬레이터가 `w`를 채널에 접어 넣은 모의 실험 (§4.4) |
| scheduler benchmark `gnb_antennas=2` (`render_single_cell.py`) | srsUE | off | port 0 | 없음 (§4.5) |
| `gnb_zmq_b210_fdd_2port_no_core.yaml` | 없음 | 기본값 | 해당 없음 | 2-port IQ 전송만 검증하는 fixture |
| OAI 2×2 (`render-oai-2x2-configs.py`, M6) | OAI nrUE 2 Rx | **on** | PDSCH는 PMI 코드북으로 두 port 사용 | srsUE가 아니고, 2×1 MISO가 아니라 2×2 SU-MIMO 구성 |

게이트 스크립트도 이 사실을 명시적으로 선언한다. `run-ocudu-rank1-2x1.sh`는 `--allow-silent-source 'gnb0->ue0:1'`, `run-ocudu-rank1-4x1.sh`와 multi-UE smoke 스크립트는 port 1–3 전부를 무음으로 선언한 뒤 DL 행렬을 판정한다.

## 4. 지금까지 측정된 것과 그 의미

### 4.1 라이브 rank-1 게이트 (2×1, 4×1)

- 실행 `20260817T091805Z`, `20260817T091856Z` (native)와 2026-08-19 컨테이너 재현.
- 측정한 것: attach, PDU, ping이 완주한다. UL은 모든 수신 행이 `y=Hx`를 1e-04 안에서 만족한다 (2/2, 4/4 branch).
- DL 판정은 무음 port를 제외하므로 **활성 branch가 1/2, 1/4**이다. DL 행 오차 3.7e-08은 "port 0 branch가 정확하고, 무음 port가 정확히 0을 기여한다"는 뜻일 뿐이다.
- MISO 관점: 조건 1 불성립.

### 4.2 합성 다중 branch 게이트 (R0/R1, 4-port 합성 캡처)

- OCUDU 대신 합성 2-port transport peer(`apps/ocudu_mimo_transport_peer.cpp`)가 모든 port에 신호를 싣는다.
- 측정한 것: DL 1×4 행 오차 1.24e-07, off-diagonal share 1.18 (네 소스 모두 기여). 위상 스윕에서 φ=0이면 진폭 2배, φ=π이면 0.
- MISO 관점: 에뮬레이터가 다중 branch 합성을 정확히 계산한다는 증거다. gNB 스택도 UE도 없으므로 링크 이득은 측정 대상이 아니다.

### 4.3 CSI-on 구성의 DL 4×1 캡처

- `scripts/web_ui/index.html`에 기록된 실행: CSI-RS를 켠 상태에서 DL 4×1 `y=Hx` 5.27e-08, 무음 선언 없이 off-diagonal share 0.081.
- 측정한 것: CSI가 켜지면 OCUDU가 실제로 port 0 이외에도 신호를 싣고, 에뮬레이터가 그것을 정확히 합성한다.
- **같은 실행에서 attach가 완료되지 않는다** (§2의 2단계). 그래서 UE 링크 품질 비교는 불가능하다.
- MISO 관점: 조건 1은 성립하지만 조건 2가 불성립.

### 4.4 Oracle 프리코딩 실험

- 고정 1×2 채널 `h = [0.80+0.10j, 0.25-0.15j]`, DL에 −30 dB AWGN 바닥. 실험자가 `h`를 알고 고른 `w`에 대해, **에뮬레이터가 유효 스칼라 채널 `c = h·w`를 port 0 채널로 선언**했다. gNB는 이 실험에서도 port 0만 송신한다.
- 측정 결과 (모두 attach, PDU, ping 완주. 수신 전력은 `|h·w|²·P_tx + noise`와 비율 1.000):

  | `w` | 예측 (MRT 대비) | 실측 |
  |---|---|---|
  | MRT `conj(h)/‖h‖` | 0 dB | 기준 |
  | port 0 단독 `[1, 0]` | −0.53 dB | −0.54 dB |
  | 동일 복제 `[1, 1]/√2` | −1.24 dB | −1.24 dB |
  | 반정합 널 | −38.7 dB | 셀 서치 실패 |

- 측정한 것: 이 `h`에서 이상적 MRT가 port 0 단독보다 **0.54 dB** 낫다. 같은 신호 복제는 port 0 단독보다도 나쁘다.
- MISO 관점: "OCUDU가 MRT를 할 수 있다면 얻었을 이득"을 에뮬레이터 쪽에서 재현한 것이다. 프리코딩을 gNB가 아니라 실험자가 채널에 접어 넣었으므로 조건 1이 불성립한다. 또 채널 하나에 대한 고정값이라 페이딩 다이버시티(차수)는 다루지 않는다. 보고서도 "closed-loop PMI를 시사하지 않는다"고 경계를 긋는다.

### 4.5 Scheduler benchmark, `gnb_antennas=2` (세션 g25)

- 세션 `20261019T111545Z-g25` (sutd_1gnb, 정적 UE 4, seed 3)를 같은 배치의 1 안테나 세션 g22와 비교했다.

  | | 1 안테나 (g22) | 2 안테나 (g25) |
  |---|---|---|
  | 셀 DL, RR / QoS-PF | 53.85 / 54.55 Mb/s | 3.08 / 3.08 Mb/s |
  | DL MCS | CQI 기반 | 모든 UE 0 |
  | UL MCS / PUSCH SNR | 28 / ~40 dB | 28 / 40–46 dB |

- DL 붕괴의 원인은 안테나가 아니라 CSI 부재다. CQI가 `initial_cqi = 3`에 고정되고, PUCCH HARQ-ACK의 약 1%가 NACK로 오판되어 OLLA가 −4까지 내려간다. srsUE 자체는 DL SNR 약 30 dB, BLER 0을 보고한다.
- MISO 관점: 조건 1 불성립. 1 안테나와 2 안테나 세션의 DL 수치 차이는 링크 적응 방식의 차이이며, 안테나 수의 효과로 읽으면 안 된다.

### 4.6 UL 1×2 / 1×4 SIMO (대조군)

- 같은 구성에서 UL은 gNB의 모든 수신 port가 각자 독립 branch를 받고, OCUDU가 MMSE 등화(`pusch_channel_equalizer_algorithm = "mmse"` 기본값)로 결합해 복호한다.
- 이 repo에서 라이브로 증명된 다중 안테나 다이버시티는 이 방향뿐이다.

## 5. 측정된 것과 측정되지 않은 것

| 항목 | 상태 | 근거 |
|---|---|---|
| 에뮬레이터의 다중 branch DL 채널 계산 정확도 | 측정됨 | §4.2, §4.3 |
| 실제 OCUDU가 CSI-on에서 여러 port로 송신 | 캡처로 확인됨 (attach 미완료) | §4.3 |
| 고정 `h`에서 이상적 MRT의 이론 이득이 라이브 링크에서 재현됨 | 측정됨 (oracle, 에뮬레이터 쪽 프리코딩) | §4.4 |
| UL 수신 다이버시티 | 측정됨 | §4.1, §4.6 |
| **OCUDU 스택이 여러 port로 송신한 신호를 srsUE가 복호** | **측정된 적 없음** | §2의 2단계 때문에 attach 불가 |
| **PMI 폐루프 프리코딩의 array gain** | **측정된 적 없음** | srsUE가 2-port CSI 보고를 하지 못함 |
| **페이딩 채널에서의 송신 다이버시티 차수 이득** | **측정된 적 없음** | 송신 다이버시티 방식 자체가 스택에 없음 |

## 6. 해석할 때 주의할 점

- README나 설정 주석의 "2×1 DL MISO"는 **에뮬레이트하는 채널의 차원**을 뜻한다. gNB가 MISO 송신을 한다는 뜻이 아니다.
- 2T 셀의 DL 성능 수치는 1T 셀 대비 이득·손실의 근거가 될 수 없다. CSI 유무에 따라 링크 적응이 달라진다는 점만 보여 준다.
- 에뮬레이터는 port 1 이상의 채널도 계산한다. 하지만 입력이 0이므로 DL 결과에 기여하지 않는다.
- DL port 1 이상에 대한 채널 모델링 오류가 있어도 현재 srsUE 라이브 실행으로는 드러나지 않는다. DL 다중 branch 검증은 합성 게이트가 맡는다.

## 7. 실제로 측정하려면

| 방법 | 내용 | 얻는 것 | 비용·위험 |
|---|---|---|---|
| A. srsUE에 2-port CSI-RS 지원 추가 | FD-CDM2 리소스 매핑과 2-port PMI/RI 보고 구현 | OCUDU의 기존 코드북 프리코딩이 그대로 동작해서, srsUE로 폐루프 MISO 이득 측정 가능 | srsUE NR PHY/RRC 수정 범위가 큼. 현재 srsUE는 DL에서 port 0 버퍼만 읽지만, 1 Rx UE의 MISO라면 UE 쪽 다중 Rx는 필요 없음 |
| B. OAI nrUE를 1 Rx로 돌려 2×1 구성 | M6 OAI 경로를 1 Rx로 렌더링 | CSI-RS on 상태에서 PMI 프리코딩 2×1 측정. 스택 수정 없이 가장 빠를 가능성이 큼 | 아직 시도하지 않음. OAI UE 경로의 attach 안정성을 먼저 확인해야 함 |
| C. OCUDU CSI-RS를 1-port로 패치 | CSI-RS port 수를 `nof_antennas_dl`과 분리 | srsUE의 CQI 복구, scheduler benchmark DL 정상화 | DL은 여전히 port 0만 송신하므로 **MISO 이득은 얻지 못함** |
| D. OCUDU에 open-loop 방식 추가 | PRG 단위 precoder cycling 등 | CSI 없이 다중 port 송신 | OCUDU 수정. 규격상 UE가 모르는 프리코딩이라 채널 추정 범위가 PRG로 제한됨. 이득 크기도 불확실 |

같은 신호를 모든 port에 복제하는 방식(`[1, 1]/√2`)은 대안이 아니다. §4.4에서 port 0 단독보다 나빴다.
