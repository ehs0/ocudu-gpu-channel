# 다중 로봇 수색·구조 데모 — 마일스톤과 검증 기록

**목표: 구조 로봇 3대가 미지의 건물을 탐색해 생존자 2명을 찾는다. 로봇마다 자기 센서와 자기 지도로 움직이고, gNB 뒤의 미션 서버가 로봇이 보낸 것만으로 지도를 합치고 탐색 구역을 나눈다. 로봇과 서버 사이의 모든 메시지는 그 로봇의 UE → 채널 에뮬레이터 → gNB를 지나고, 로봇의 위치가 Sionna RT 채널을 바꾼다(폐루프). 에뮬레이터가 이 데모에서 보여 주는 것은 "통신이 끊긴 로봇이 무엇을 알고, 중앙이 무엇을 언제 알게 되는가"가 무선 경로에 따라 달라진다는 것이다.**

요구 명세: 사용자 제공 `multi_robot_rescue_gpu_channel_demo_spec.md`(2026-10-15). 단계 번호는 `MR`. 판정 규율은 R 트랙과 같다: exit≠0만으로 판정하지 않고 로그에서 메커니즘을 확인하고, 실패 측정을 보존한다. 용어는 [예제 README](examples/multi_robot_rescue/README.md#terms)의 표를 따른다(link state, backlog, local confirmation, central report, perception mock, network mode, world).

## 음영 지역 정책 — 사용자 결정(변경 금지)

통신을 잃은 로봇은 **혼자 계속 탐색한다**. 커버리지로 되돌아가지 않고, 경계에서 기다리지 않고, 멈추지 않는다. UE 링크는 백그라운드에서 재접속을 계속 시도하고, 서버가 다시 답하면 그동안 쌓은 기록을 생존자 보고부터 보낸다. 릴레이 로봇, sidelink, 수동 복구는 범위 밖이다.

| 구성 요소 | 링크가 끊기면 |
|---|---|
| `robot.py` (로컬 자율) | 그대로: 자기 frontier를 고르고, 지도와 후보를 계속 만든다. 링크 상태는 기록만 하고 결정에 쓰지 않는다 |
| `ue_link.py` (UE 링크) | `OFFLINE`: 데이터 전송 중단, 이벤트는 SQLite 큐에 저장, `resume_hello`를 1→2→4→8 s 백오프로 계속 보낸다 |
| `mission_server.py` | 그 로봇에 아무것도 보내지 않는다. 마지막 자세와 나이를 표시하고, 마지막 위치 근처는 다른 로봇에 덜 배정한다 |
| `sim.py` (월드) | 계속(wall-clock). 링크를 기다리지 않는다 |

## M0 — 구조 적합성 감사 (2026-10-15)

**작업 위치와 이름.** 스펙의 `use_cases/`는 이 저장소에 없다: 예제는 `examples/<name>/`에 평탄하게 두고(`robot_fight`, `scheduler_benchmark`, 브랜치의 `v2x_intersection`), Python 모듈은 `snake_case.py`, 실행 스크립트는 `run-ocudu-<name>.sh`, 게이트 훅은 `launch/root_exec.sh`·`launch/ue_exec.sh`, 씬·시나리오는 `examples/configs/sionna/{scenes,scenarios}/<name>/`, 테스트는 `tests/test_<demo>_*.py`, 마일스톤 문서는 루트 `<NAME>_MILESTONES.md`. 이 데모는 그대로 따른다: `examples/multi_robot_rescue/`, `run-ocudu-multi-robot-rescue.sh`, `tests/test_rescue_*.py`, 이 문서. 스펙이 제안한 하위 폴더(`robots/`, `edge/`, `network/`…)와 YAML 설정층은 만들지 않았다(다른 예제와 같이 CLI 인자 + 환경 변수).

**재사용한 것.**

| 재사용 | 출처 | 이 데모에서 |
|---|---|---|
| 네이티브 멀티-UE Sionna 게이트와 훅 `OCUDU_NATIVE_MUE_ROOT_EXEC` / `_UE_EXEC` | `scripts/native/run-ocudu-sionna-multi-ue.sh`, `run-ocudu-multi-ue-inner.sh` | 수정 없이 사용. UE 수는 2 또는 4만 지원 → 로봇 1–2대는 2-UE, 3대는 4-UE(남는 UE는 건물 밖 LOS에 parked) |
| 브리지 외부 위치 입력 `ExternalPositionSource` (`frame: arena`, latest-wins, stale 표시) | `scripts/sionna_rt/run_bridge.py` | `sim.py`가 같은 형식으로 `ue0..2`를 발행. `robots`·`referee` 필드는 브리지가 무시 |
| UE 네임스페이스 안 stdlib 프로세스 + unix 소켓 릴레이 패턴 | `robot_fight/modem.py`, V2X `ue_client.py` | `ue_link.py`가 TUN 주소에 바인드하고 로봇과는 unix 소켓 |
| 씬 생성(PLY + Mitsuba XML, ITU 재질) | `scenes/robot_ring/build_robot_ring.py`, V2X 씬 빌더 | `build_multi_robot_rescue.py`, PLY writer 재사용 |
| 외부 의존성 규칙(저장소 밖 설치, README 표, 어댑터 1개) | `AGENT_HARNESS.md`, V2X | Isaac Sim은 `isaac_world.py`만 import |

**이 호스트 (DGX Spark GB10, aarch64, Ubuntu 24.04, Python 3.12, 컨테이너).** root·docker 없음(`apt-get download` + `dpkg-deb -x`로 패키지 파일을 받아 푸는 것만 가능, 설치는 불가). Sionna RT 2.0.1(`~/venvs/sionna`), 네이티브 워크스페이스 `~/ocudu-spark`, 격리 런처 `~/ocudu-setup/run-isolated*.sh`(sudo 필요). Isaac Sim은 설치돼 있지 않았다: pypi.nvidia.com의 `isaacsim 6.2.0.0` aarch64 cp312 휠로 `~/venvs/rescue`에 사용자 공간 설치(사용자가 EULA 동의, 20 GB). ROS 2 / Nav2는 시스템 패키지가 필요해 설치 불가 → 사용자 결정으로 **ROS 없이 순수 Python** 로컬 자율.

**능력 매트릭스.**

| 항목 | 상태 | 근거 |
|---|---|---|
| 3로봇 탐색 + 오프라인 큐 + 재동기화, 루프백 | **검증(개발 모드)** | 아래 MR1–MR2, 테스트 |
| 로봇 위치 → Sionna 브리지(dry-run, 브로커 없음) | **검증** | MR2 브리지 dry-run |
| 씬에서 N4가 RT 채널상 음영 | **검증(채널)** | `channel_profile.py`; 링크 결과는 라이브에서 측정해야 함 |
| 라이브 1 UE 데이터 경로(MR3) | **미실행** | 라이브 게이트는 root 필요; 이 세션에 sudo 없음 |
| 라이브 2/3 UE (MR4) | **미실행**, 기존 기록상 위험 | 움직이는 2-UE 연속 트래픽은 저장소 상태에서 실패로 기록됨(`AGENT_PROGRESS.md`) |
| srsUE 깊은 페이드 후 자동 복구 | **의존성, 미검증** | V2X 34 dB 런에서 ue0가 재접속하지 못함; 게이트의 srsUE 리비전(`eea87b1d`)과 복구 패치(`daa167ae3`)의 관계 미확인 |
| Isaac Sim 월드(`isaac_world.py`) | **검증(루프백 에피소드 1회)** | `r7-isaac`: 성공, RTF 1.000; 카메라 기반 인식은 없음(perception mock) |
| rank-1 다중 포트 / 동일 PRB MU-MIMO | **범위 밖 / 주장 안 함** | 단일 포트 UE, gNB 1포트 |

## 단계

| 단계 | 내용 | Exit 게이트 | 상태 |
|---|---|---|---|
| **MR0** | 감사 — 레포 규칙, 재사용, 호스트, 능력 매트릭스 | 검증된 경로·명령만 문서화 | **완료 2026-10-15** (위 절) |
| **MR1** | 월드와 로컬 자율 — 3로봇, 점유격자, frontier, 반응형 안전층, perception mock, 정답 분리 | 링크를 끊은 robot_2가 계속 움직이고 생존자를 찾는다 | **완료(개발 모드) 2026-10-15** — kinematic과 Isaac Sim 6.2 두 월드, 측정은 아래 |
| **MR2** | 미션 서버 + 오프라인 동기화 — 융합, 배정(commitment), ACK/중복 제거, resume, TTL, 대시보드, 분석 | 20 s 끊김: 로봇 계속 활동, 서버는 복구 전까지 모름, 모든 이벤트 정확히 1회 | **완료(루프백) 2026-10-15** |
| **MR3** | 라이브 1 로봇 — `OCUDU_NATIVE_RESCUE_ROBOTS=1`, 양방향 앱 seq/ACK, 다른 경로 차단, RF로 인한 끊김과 진짜 복구 | 꾸며낸 `connected`가 아닌 측정된 복구, 또는 실패 기록 | **부분 2026-10-15** — 2차 런에서 실제 5G 경로로 앱 전달·DEGRADED→ONLINE 복귀를 측정. RRC Release 후 srsUE(`eea87b1d`)가 재접속하지 않아 **진짜 복구는 미달**(radio-stack 한계, 아래) |
| **MR4** | 라이브 2 → 3 로봇 | 지원 동시 수 선언 | 미실행 |
| **MR5** | 통합 에피소드 + 기준선 A/B/C, 시드 반복 | 가장 높은 검증 모드에서 재현 가능한 1회 이상 | 기준선 A·B와 결함 주입은 동작; C(라이브) 미실행 |
| **MR6** | 선택: MU-MIMO, 학습형 정책, LLM 어댑터, 다층 | — | 범위 밖 |

## 설계 요약

```
sim.py ──sensor (unix)──▶ robot.py ──event (unix)──▶ ue_link.py ══UDP/TUN══ srsUE ═IQ═ broker ═ gNB ═ UPF ══▶ mission_server.py
   │   ◀──cmd (unix)───        ◀──task (unix)──            (UE netns)                               (10.45.1.1:7200)
   └──positions (ZMQ PUB)──▶ Sionna bridge ──▶ broker control plane          (referee/dashboard도 구독; 서버·로봇은 구독 안 함)
```

- **정보 분리.** 로봇: 자기 오도메트리, 2-D 스캔, perception mock의 방위·거리(정체 없음). 서버: UDP로 받은 것만. 정답(생존자 위치, 진짜 자유 공간)은 `sim.py`의 심판과 대시보드의 심판 뷰만. `tests/test_rescue_isolation.py`가 소스 수준에서 확인(로봇에 AF_INET 없음, 서버에 zmq·월드 import 없음, 정답 함수 참조 없음).
- **링크 상태.** 서버 응답(heartbeat/ACK/resume_ack)의 나이로만 정한다: 3 s DEGRADED, 5 s OFFLINE, 응답 2회 연속이면 DEGRADED→ONLINE, OFFLINE→(resume_ack)→SYNCING→(백로그 0)→ONLINE. TUN 존재는 따로 기록하고 상태를 ONLINE으로 만들지 않는다.
- **오프라인 큐.** SQLite(WAL), 우선순위 생존자 0 > 과제 ACK 1 > 지도 타일 2 > 자세·링크 이벤트 3. 자세·타일은 키별 최신만 유지(coalesce). 128 MiB 초과 시 낮은 우선순위부터 축출, 생존자 보고는 축출하지 않음. 프로세스 재시작에도 남는다.
- **지도.** 0.2 m 격자, 16×16셀 타일 단위로 "바뀐 타일 전체"를 보낸다 → 기준 리비전이 없는 자기완결 메시지, 서버는 (로봇, 타일)별 capture 시각이 새로운 것만 반영. 스펙의 delta+keyframe 대신 택한 설계.
- **과제.** revision·`valid_until_ms`(10 s)·command_id. 로봇은 다른 로봇/미션, 낡은 revision, 만료, 최근 막혔거나 도달한 목표를 거절한다. 서버는 수락된 과제의 목표가 아직 frontier인 동안 바꾸지 않고(commitment) 만료 3 s 전에 같은 목표로 갱신한다. 거절된 목표는 그 로봇에 60 s 다시 주지 않는다.
- **시계.** 모든 프로세스가 한 호스트 시계(wall ms, monotonic). 편도 시간(capture→수신→ACK)은 같은 시계이므로 유효.

## 진행 기록

### MR0–MR2 — 2026-10-15 (GB10 컨테이너, 루프백, kinematic world)

**구현.** `examples/multi_robot_rescue/` 전체, 씬·시나리오(`entrance`/`street` × 1/2/3 로봇), 라이브 게이트와 훅, 대시보드, 분석기, 테스트 8개 파일. Isaac Sim 설치는 사용자 공간 우회 세 가지가 필요했다(소스 빌드용 Python 헤더, Vulkan ICD가 링크하는 X11/GL 라이브러리, libgomp 프리로드); `setup-external.sh --isaac`가 root 없이 재현하며 래퍼 `rescue-python`을 쓴다. 재실행으로 확인한 것은 이미 설치된 venv 위의 경로뿐이고, 빈 venv에서의 소스 빌드는 이 스크립트로 다시 돌리지 않았다.

**씬 검증 — 벽 모델링 결함을 찾고 고침.** 처음엔 벽을 닫힌 박스로 내보냈다. Sionna ITU 재질은 면 하나를 `thickness` 두께의 슬래브로 모델링하므로 박스 벽은 통과마다 상호작용 2회·감쇠 2배가 되어, `max_depth 3`에서 방 대부분이 경로 없음이었다(직접 탐침: N3·S3·S4·N4 0 paths). 벽을 중심선의 단일 면으로 바꾸자 같은 지점에 경로가 생겼다(N3 −115 dB, S3 −113 dB 물리 손실, N4는 여전히 없음). 잔해는 닫힌 박스 유지.

**채널 프로파일** (`channel_profile.py`, 2 m 격자, 브리지 기본 `--gain-offset-db 60`, 1.8425 GHz, 갱신 1회 추적 p50 ≈ 98 ms, 1 UE): 가장 강한 DL 탭 중앙값

| 사이트 | 복도 | 인접 방 N1/N2/S1/S2 | 먼 방 N3/S3/S4 | N4 (강철) |
|---|---|---|---|---|
| `entrance` (−1, 12, 2.5) | −0.4 dB (최대 +13.2) | −9…−14 dB | −23…−26 dB | **경로 없음**(문 옆만 최대 −5.7 dB) |
| `street` (−12, 12, 4) | −4.4 dB | −23…−25 dB | −34 dB, S4 경로 없음 | 경로 없음 |

V2X 측정(40 dB 잡음 바닥에서 −35 dB 탭은 BLER 98 %, 34 dB 바닥에서 링크 상실)에 비추면 `entrance`의 N4는 끊김, 먼 방은 경계, 복도·인접 방은 연결이 예상된다 — **예상일 뿐, 링크 결과는 MR3에서 측정한다.** 복도 탭 최대 +13 dB는 ZMQ int16 포화 위험(UL 포화 선례)이 있어 MR3에서 gNB 입력 레벨을 확인해야 한다.

**브리지 dry-run** (`run_local.py --bridge …-3robot.json`, 90 s, 4 UE·8 링크): 위치 소스 `external`(ue0–2), ue3 `scripted`, feed 806건·무효 0, 갱신 672회, 추적 p50 130 ms / p95 163 ms(목표 10 Hz에 대해 실효 ≈ 7 Hz), 로봇 위치별 DL 탭 중앙값 복도 +1.9 dB, N1 −5.4, S1 −7.1, S2 −14.3 dB. 로봇 → Sionna 절반의 루프가 이어진다.

**루프백 에피소드** — 결과 표는 아래 *측정 결과* 절.

**내비게이션 결함과 수정(측정으로 발견).**
- 안전층 통로가 플래너 인플레이션보다 엄격 → 모서리마다 정지·회전 교착(robot_1 안전 정지 2,158회, 37회 stuck). 안전층을 로봇 폭 통로 검사로 바꾸고 플래너의 회전을 유지.
- 생존자(원판 장애물) 앞에 끼어 80 s부터 끝까지 정지 → 1.5 s 막히면 후방이 비었을 때 1.2 s 후진.
- 서버가 로봇이 `stuck`으로 거절한 목표를 반복 배정 → 거절 목표 60 s 회피.
- 서버가 2 s마다 목표를 바꿔 로봇이 아무 데도 도달하지 못함 → commitment.
- 갱신된 같은 목표로 왕복하며 6 s 0.3 m 이동 기준을 피함, 벽을 스치는 빔이 벽 셀을 free로 깎아 벽 위 가짜 frontier → stuck을 "같은 목표까지 0.3 m 진전 없이 8 s"로, free 마킹을 충돌점 한 셀 앞에서 중단, 인플레이션을 하드 1셀 + 비용 4배 소프트 1셀로.

### MR3 1차 — 2026-10-15 (`20261015T110103Z`, GB10, 라이브 1 로봇, 실패)

`sudo env RESCUE_ROBOTS=1 bash ~/ocudu-setup/run-isolated-rescue.sh`. 게이트 판정 `failed`: ue0 RRC·PDU·ping 3/3 후 SR 실패 105회·재접속 시도 722회, parked ue1은 PDU 세션을 끝내 못 맺음. 데모: robot_1의 링크는 46.6 s에 마지막 응답, 51.6 s OFFLINE, 끝까지 복구 없음. robot_1은 그동안 혼자 탐색해 **두 생존자를 모두 오프라인에서 확인**(138.7 s, 439.1 s)했지만 보고 2건을 포함한 백로그 104건이 전달되지 못했고, 에피소드는 600 s 시한 실패(중앙 커버리지 0.22).

- **원인: UL 보정 오류.** 끊긴 순간 채널은 DL −8 / UL −5.9 dB(잡음 바닥 40 dB 기준 SNR > 30 dB)였고, 이후 +4…+6 dB 복도에서도 복구되지 않았다 — 채널 페이드가 아니다. gNB PRACH rssi가 **+14.9 dB**로, 통과한 최근 V2X 런들(+2.7, −17.3 dB)보다 12 dB 이상 높았다. `~/ocudu-setup/run-rescue.sh`가 로봇/V2X 런의 `OCUDU_NATIVE_SIONNA_TX_POWER_UL=3.0e2`를 복사해 UE 스케일을 렌더러의 측정 보정(3.0e4)보다 20 dB 올렸고, 입구 gNB의 탭은 최대 +12 dB라 gNB의 ZMQ int16 입력이 포화되었다(UL 포화 선례와 같은 메커니즘). 증상: SR 미수신 → RRC 해제 → PRACH 724회, Msg3 미완료.
- **수정.** `run-rescue.sh`는 기본 보정(3.0e4)을 쓰고 `RESCUE_TX_POWER_UL`로만 바꾼다. 예상 PRACH rssi ≈ −5 dB. 재실행에서 이 값을 먼저 확인한다.
- **측정 결함.** `ue_link.py`의 TUN 감지가 `/sys/class/net`을 읽어 `nsenter --net` 아래에서 항상 false였다(부모 네임스페이스의 sysfs). `socket.if_nameindex()`로 고쳤다. 링크 상태 판정에는 쓰이지 않으므로 이 런의 상태 기록은 유효하다.

### MR3 2차 — 2026-10-15 (`20261015T112725Z`, GB10, 라이브 1 로봇, UL 기본 보정)

게이트 `failed`(ue0 `attach_clean=0`: SR 실패 2, 재접속 시도 11; ue1 깨끗하게 PDU·ping 3/3). gNB PRACH rssi **+3.2 dB**(1차 +14.9 dB) — 포화 해소. 데모 에피소드: 600 s 시한 실패, 중앙 커버리지 0.48, 생존자 1/2 보고.

- **실제 5G 경로 위의 앱 전달 (0–230 s).** robot_1의 UE 링크는 227 s ONLINE. capture→ACK 지연(UE→에뮬레이터→gNB→UPF→서버→ACK 왕복): 지도 타일 675건 p50 43 ms / p95 64 ms / 최대 1.6 s, 자세 220건 p50 33 ms, 과제 ACK 42건 p50 65 ms. S2 생존자: 로컬 확인 115.24 s → 중앙 수신 115.25 s(capture→ACK 0.41 s). 중앙 과제 42건이 하향으로 전달·수락됨.
- **N2 방의 약한 채널은 견딤.** 151.7–217.5 s N2에서 DL/UL 탭 −11…−34 dB(잡음 바닥 40 dB)였지만 링크는 ONLINE 유지.
- **끊김.** 221.2 s DEGRADED → 222.0 s ONLINE(응답 2회, 히스테리시스 동작) → 228.0 s DEGRADED → 230.4 s OFFLINE. srsUE: 219 s(11:31:24) PHY `SYNC: detected out-of-sync... skipping slot` 시작 → `Scheduling request failed: releasing RRC connection` → PRACH 재시도 → 225 s `Received RRC Release`. 이 시점 로봇은 N2 문을 나와 복도에 있었고 탭은 −15 → +3 dB로 1.4 s 만에 18 dB 상승한 직후였다. 깊은 페이드가 아니라 **급격한 채널 변화 직후의 UE 동기 상실**이다(브로커 starvation은 231–234 s에 초당 2–5, 끊김 이후). 원인 확정은 미완: 탭 지연 구조 변화(geometry 변경 시 history 리셋)와의 관계를 아직 보지 않았다.
- **복구 없음 = radio-stack 한계.** RRC Release 뒤 srsUE `eea87b1d`는 종료(11:42)까지 11분간 재접속을 시도하지 않았다(ue_link의 probe가 상향 데이터를 계속 냈음에도). `AGENT_PROGRESS.md`의 SA 복구 패치(`c70ae415b`…`daa167ae3`, 별도 UE 체크아웃)는 이 호스트와 원격 포크(`master`, `prach-preamble-index`만 공개)에 없다. 스펙의 bounded supervisor(UE 재시작) 우회는 구현하지 않았다 — 사용자 결정 대기.
- **음영 정책은 동작.** robot_1은 230–601 s OFFLINE 동안 N1·N3·N4·S4·S3를 계속 탐색해 N4 생존자를 424.8 s에 오프라인에서 확인했고, 보고는 백로그(최대 80건)에 남았다.
- **부수 관찰.** srsUE internal 로그가 3.5 GB(out-of-sync 초당 ~470줄) — 긴 런에서 디스크 위험.

### srsUE 동기 상실 조사 — 2026-10-15 (사용자 결정: srsUE 한계로 확정, 원인만 조사)

**결정.** 재접속 실패는 이 srsUE(`eea87b1d`)의 한계로 기록한다. UE 재시작 감시나 다른 UE 빌드로 우회하지 않는다.

**동기 상실 순간에 채널은 평범했다** (`20261015T112725Z`, 첫 out-of-sync 11:31:24.294, 에피소드 ≈ 219 s, 브로커 t ≈ 225 s):

- 직전까지 UE는 PUCCH로 CQI 15를 보고했다(11:31:24.287, 마지막 보고). 7 ms 뒤 열화 없이 `SYNC: detected out-of-sync... skipping slot`가 나왔고, 0.1 s 뒤부터 `PBCH-MIB: NR SFN (536) does not match current SFN (531)`가 끝까지 이어졌다. UE의 프레임 카운터가 정확히 5프레임(50 ms) 뒤처진 채 고쳐지지 않았다. 진행 기록의 `94cd6e71d`(decoded-slot realignment)가 다루는 증상이며 이 빌드에는 없다. gNB는 `MAC max consecutive undecoded CSIs`로 RLF를 선언했고 225 s에 RRC Release를 보냈다.
- 그 순간 DL 탭은 +2…+3 dB, 가장 센 경로 지연은 1.12–1.19 샘플로 안정(연결 기간 전체에서 2 샘플 이상의 점프 0회). 직전 1.4 s 동안 탭 수 9 → 17, 마지막 탭 지연 2.2 → 7.25 샘플이 되었다. 그러나 끊기기 전 연결 상태에서도 탭 15개 이상이 192회, 마지막 지연 7 샘플 이상이 45회, 한 갱신에 최대 20.3 dB의 이득 변화가 있었고 모두 견뎠다. **채널 품질·타이밍·급변의 크기로는 설명되지 않는다.**
- 브로커 starvation은 상실 순간(t ≈ 224–226)에는 없었다. 세 장치 동시 burst(231, 234 s)는 상실 뒤, 재접속 시도 구간이다.

**모든 상실은 움직이는 UE에서만.** 첫 out-of-sync가 있는 런: rescue 1차 ue0(SFN 5프레임 차), rescue 2차 ue0(5프레임), V2X `20261014T102328Z` ue0(1프레임), `20261014T104052Z` ue0(2프레임). 같은 런의 parked ue1과 통과한 V2X 런들에서는 0회. rescue 두 런 모두 첫 out-of-sync 2–6 s 뒤에 앱 링크가 끊겼다.

**움직이는 UE에만 있는 것: 이력 리셋.** 지연 구조가 바뀌는 채널 갱신마다 브로커는 그 edge의 지연선 링을 0으로 비운다(`physical_link.h`, `history_reset_required`; warmup = 최대 탭 지연 + 분수 지연 필터 길이, 수 샘플). 이 런에서 gnb0→ue0 리셋은 초당 평균 3.1회(최대 9)였고, parked UE의 채널은 바뀌지 않아 리셋이 없다. 불연속은 슬롯당 수 샘플로 작아 대부분 무해하지만, **동기 추적에 걸리는 드문 사건인지는 아직 가설이다** — 브로커 로그에 벽시계가 없어 리셋과 상실을 1초보다 좁게 맞출 수 없었다.

**인과를 가르는 다음 실험(미실행).**
1. *같은 갱신, 서 있는 UE:* scheduler_benchmark의 fan-out처럼 ue1에 ue0와 똑같은 채널 갱신 열을 주는 1로봇 런. ue1도 미끄러지면 갱신/리셋 원인, 아니면 UE 쪽 타이밍.
2. *리셋 없는 움직임:* 같은 경로를 지연 구조 고정(경로 수와 지연을 고정하고 이득·위상만 갱신 = history 보존 경로)으로 재생.
3. 브로커 이벤트에 벽시계(또는 슬롯↔unix ms) 기록 — 리셋과 SSB 슬롯의 일치 여부를 슬롯 단위로 본다.

### 라이브 Isaac 1차 — 2026-10-15 (`20261015T121010Z`, 중단)

- 대시보드: 08:13에 띄운 V2X 대시보드(root)가 8080을 점유해 rescue 대시보드가 `Address already in use`로 죽었고, 사용자는 V2X 페이지를 보았다. 게이트 스크립트는 이제 포트가 쓰이고 있으면 시작 전에 중단한다.
- Isaac: 게이트의 root는 사용자 네임스페이스 안에 있어 venv 안 Kit 캐시(dev 소유)에 쓰지 못했다(`Failed to create local file data store ... DerivedDataCache`). 렌더마다 셰이더를 다시 만들며 물리 루프가 sim 시간 5 s에서 멈췄다(`carb.tasking` 100 %). 렌더가 실시간 물리를 막을 수 있던 설계 결함이다. 수정: `root_exec.sh`가 런 디렉터리에 Kit `--portable-root`를 만들고 venv의 따뜻한 캐시(542 MB)로 채운다. `isaac_world.py`는 에피소드 시계 전에 카메라를 워밍업한다(dev 사용자 검증: 0.88 s, 이후 렌더 ≈ 14 ms). 네임스페이스 root에서의 재검증은 다음 런.

## 측정 결과

플랫폼: DGX Spark GB10 컨테이너, 루프백(무선 없음), 시드 20261015, 생존자 `demo`(N4 안쪽 구석 + S2), 시한 600 s, 커버리지 목표 0.80. 각 행은 한 에피소드(편차가 크므로 모드 비교의 근거가 아니라 메커니즘 확인용). 증거: `~/ocudu-spark/results/rescue-local/20261015/<run>/episodes/*/`(`summary.json`, `sim/`, `links/`, `server/`, `robots/`). 시각은 에피소드 시작 기준 초.

| 런 | network mode / world | 결과 | 커버리지 80 % | S2 생존자: 로컬 확인 → 중앙 수신 | N4 생존자: 로컬 확인 → 중앙 수신 | 끊긴 링크 |
|---|---|---|---|---|---|---|
| `r6-ideal` | ideal / kinematic | 성공 223.8 s | 169.8 s | 104.6 → 104.6 (robot_3) | 222.1 → 222.1 (robot_3, ONLINE) | 없음 |
| `r6-blackout` | 결함 주입 robot_2 40–60 s / kinematic | 성공 191.8 s | 147.8 s | 162.4 → 162.4 | 190.4 → 190.4 (robot_3) | robot_2 OFFLINE 44.7–59.9 s, 백로그 최대 6 |
| `r6-shadow` | 단순 기준선(N4 안 전부 손실) / kinematic | 성공 330.2 s | 204.8 s | 86.0 → 86.0 | **287.3 (robot_3, OFFLINE) → 328.2 (robot_2의 293.6 보고)**, 40.9 s 지연 | robot_2 273.0–328.2 s, robot_3 281.5 s–끝 |
| `r7-isaac` | 결함 주입 robot_2 / **Isaac Sim 6.2** | 성공 335.3 s, RTF 1.000, late step 0 | 302.7 s | 82.6 → 82.6 | 333.8 → 333.9 (robot_2, ONLINE) | robot_2 OFFLINE 37.9–61.1 s, 그동안 7.0 m 이동 |

**음영 정책의 확인 (r6-shadow).** robot_3(276.7 s)과 robot_2(268.0 s)가 N4에 들어가 각각 5 s 뒤 OFFLINE이 되었고, 둘 다 오프라인에서 생존자를 확인했다. robot_2는 되돌아가라는 규칙 없이 자기 frontier를 따라 325.7 s에 복도로 나왔고, 다음 `resume_hello`(백오프 단계)에 서버가 답해 328.2 s에 SYNCING → ONLINE(백로그 23건, 루프백이라 수 ms에 배수)이 되었다. 생존자 보고는 원래 capture 시각 293.6 s를 지닌 채 328.2 s에 한 번 도착했고(send_count 1), 그 순간 두 생존자가 모두 중앙에 보고되어 에피소드가 끝났다. robot_3은 끝까지 N4 안에 있었고 그 보고는 이 에피소드 안에 도착하지 못했다. 서버는 블랙아웃 동안 해당 로봇의 데이터를 0건 받았다(`r6-blackout`: 프록시 차단 구간 내 서버 수신 0; `tests/test_rescue_episode.py`가 같은 검사).

**오프라인 중 이동 (`r6-blackout`, `r7-isaac`).** robot_2는 OFFLINE 15.2 s 동안 7.5 m(kinematic, 이전 `run-bo1` 런), 23.2 s 동안 7.0 m(Isaac) 움직였고, 그동안 자기 frontier 목표를 매초 갱신했다(`goal source=local`). 재접속은 차단 종료 뒤 첫 probe에서 일어난다: probe 간격이 1→2→4→8 s로 늘어 차단 종료 후 최대 8 s가 걸린다(r6-blackout: 차단 종료 ≈ 60 s, 재접속 59.9 s).

**수정 전 실패 런(보존).** `run-ideal2`: 시한 360 s, 커버리지 0.78, robot_1이 S2 생존자 앞에서 280 s 정지(후진 회복 도입 전). `r4-shadow`: robot_3이 351 s에 N4 진입, 363.8 s 오프라인 확인, 시한 400 s 안에 나오지 못해 **실패** — 로컬 확인과 중앙 보고가 다른 지표인 이유의 예. `r5-shadow`: 600 s 동안 아무도 N4에 들어가지 않음(서버 지시가 2 s마다 바뀌던 시기; commitment 도입 전).

**Isaac 월드 (GB10).** `isaacsim 6.2.0.0`, 기동 6–8 s, 앱 GPU 메모리 1.1 GB(씬 실행 중 2.3 GB). 3로봇 + Raycast 2-D 라이다 180빔: 50 Hz 물리에서 1.67 ms/step(render=False, RTF 12). render=True는 ≈ 10 ms/step(RTF 1.6). 라이다는 기하 레이캐스트와 빔 중앙값 오차 0. 광선을 몸체 안에서 시작하면 모든 빔이 자기 섀시에 걸리므로(측정: 0.05 m) 0.27 m 밖에서 시작한다. NVIDIA 로봇 asset은 인터넷 asset 서버에서만 받을 수 있어 쓰지 않고 기본 도형 로봇을 쓴다. 접촉(충돌) 수는 Isaac에서 세지 않는다(N/A).

**테스트.** `tests/test_rescue_*.py` 56개 통과(robot venv, 100 s; 단위 50개는 rescue venv에서도 통과). 정전 복구 2개와 에피소드 4개는 실제 프로세스를 wall-clock으로 돌린다(`RESCUE_SKIP_WALLCLOCK=1`로 끔).

## 위험과 한계

- **라이브 미실행.** MR3–MR5의 라이브 단계는 root가 필요한 게이트를 이 세션에서 돌리지 못했다. 라이브 판정은 게이트의 `attach-summary.json`과 이 데모의 `summary.json`을 함께 본다.
- **움직이는 다중 UE.** 저장소 상태 기록상 움직이는 2-UE 연속 트래픽은 실패(UE1 간헐), strict 실시간은 미달. 3로봇(4 UE)은 그보다 어렵다. 지원 동시 수는 측정 후 선언한다.
- **srsUE 복구.** 깊은 페이드 뒤 자동 재접속이 안 되면 그것을 radio-stack 한계로 기록한다. UE 재시작 같은 우회는 구현하지 않았다(미션의 "결과를 좋게 보이게 하는 fallback 금지").
- **GPU 공유.** 브로커, Sionna(4 UE 추적 p50 130 ms), Isaac(물리 1.7 ms/step, 렌더링 끔)이 한 GB10을 쓴다. 라이브에서 Isaac이 브로커 슬롯 데드라인에 주는 영향은 측정 전이다.
- **라이브 게이트의 인터프리터.** 게이트는 root로 돌므로 `HOME` 기반 기본값이 틀릴 수 있다. `RESCUE_PYTHON=/home/dev/venvs/rescue/bin/rescue-python`을 명시한다(`~/ocudu-setup/run-v2x.sh`와 같은 방식).
- **월드.** kinematic world는 동역학이 없고 오도메트리가 정확하다(SLAM 드리프트 없음). 결과는 개발 모드다.
- **perception mock**은 정답을 읽는 기하 판정이다. 인식 정확도 주장은 없다.
- **중앙 플래너**는 규칙 기반(탐욕 frontier 배정). 큰 모델 어댑터는 만들지 않았다: 어떤 결과도 그것에 의존하지 않는다.
- **에피소드 편차.** 같은 시드에서도 프로세스 타이밍이 달라 탐색 순서가 바뀐다. 단일 런으로 모드를 비교하지 않는다.
