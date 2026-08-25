# OCUDU gNB–UE, Sionna RT, Web UI 실행 및 코드 설명

이 문서는 현재 저장소에서 다음 1×1 구성을 실행하는 최종 명령과 각 구성요소의 기능을 설명한다.

```text
                           Sionna RT
                               │ 채널 프로파일 갱신
                               ▼
Open5GS 5GC ── OCUDU gNB ⇄ CUDA Channel Broker ⇄ srsUE
                               │
                               ├── Broker telemetry
                               └── Web UI (read-only)
```

이 실행 경로는 Docker를 사용하지 않는다. 일반 사용자 권한으로 격리된 user/network/mount namespace를 만들고, 그 안에서 gNB, UE, 5GC와 채널 에뮬레이터를 실행한다.

## 1. 최종 실행 명령

아래 블록을 터미널에 그대로 붙여 넣는다.

```bash
cd /home/ubuntu/OCUDU/ocudu-gpu-channel

export OCUDU_NATIVE_ROOT=/home/ubuntu/ocudu-native-workspace
export CUDACXX=/opt/conda/envs/torch/bin/nvcc
export OCUDU_NATIVE_GPU_DEVICE=0
export OCUDU_NATIVE_WEB_PORT=8080

./scripts/native/run-ocudu-sionna-1x1.sh
```

정상적으로 준비되면 다음 메시지가 출력된다.

```text
event=native_sionna_1x1_live_ready
Web UI: http://127.0.0.1:8080
The live demo keeps running until Ctrl-C.
```

Web UI 주소:

```text
http://127.0.0.1:8080
```

실행을 종료할 때는 실행 터미널에서 `Ctrl+C`를 한 번 누른다. 런처가 Web UI, Sionna, GPU Channel Broker, srsUE, OCUDU gNB, Open5GS, MongoDB를 함께 종료하고 임시 네트워크 및 mount 상태를 정리한다.

## 2. 원격 서버에서 Web UI 접속

프로그램이 원격 GPU 서버에서 실행 중이라면 로컬 PC에서 SSH 포트 포워딩을 연다.

```bash
ssh -L 8080:127.0.0.1:8080 ubuntu@GPU_SERVER_IP
```

그다음 로컬 브라우저에서 다음 주소를 연다.

```text
http://127.0.0.1:8080
```

Web UI 서버는 보안을 위해 원격 인터페이스에 직접 공개되지 않고 서버의 loopback 주소에만 바인딩된다.

## 3. 환경변수의 의미

| 환경변수 | 역할 |
|---|---|
| `OCUDU_NATIVE_ROOT` | 고정된 OCUDU, srsUE, Open5GS, MongoDB 바이너리와 실행 결과를 보관하는 native workspace |
| `CUDACXX` | CUDA 코드를 빌드하는 `nvcc` 실행 파일 |
| `OCUDU_NATIVE_GPU_DEVICE` | CUDA Channel Broker와 Sionna가 사용할 물리 GPU 번호 |
| `OCUDU_NATIVE_WEB_PORT` | Web UI HTTP 포트, 기본값은 `8080` |

자동 종료가 필요하면 실행 시간을 초 단위로 지정할 수 있다.

```bash
export OCUDU_NATIVE_SIONNA_DURATION_SECONDS=150
./scripts/native/run-ocudu-sionna-1x1.sh
```

값이 없거나 `0`이면 `Ctrl+C`를 누를 때까지 계속 실행된다.

## 4. 실행 스크립트의 역할

### `run-ocudu-sionna-1x1.sh`

Sionna 실행 모드를 선택하는 최상위 진입점이다. `OCUDU_NATIVE_CHANNEL_MODE=sionna`를 설정한 뒤 공통 1×1 런처를 실행한다.

### `run-ocudu-legacy-1x1.sh`

전체 실행을 관리하는 supervisor다. 주요 역할은 다음과 같다.

1. native workspace의 고정 revision과 파일 hash를 검증한다.
2. gNB, UE, Open5GS, subscriber, Broker 설정을 실행별 디렉터리에 생성한다.
3. 현재 checkout의 `ocudu-gpu-channel`을 CUDA 활성화 상태로 빌드한다.
4. CTest와 CUDA hardware probe를 통과해야만 실제 무선 스택을 시작한다.
5. 격리된 namespace runtime과 Web UI를 실행하고 상태를 감시한다.
6. RRC 연결, PDU session, ping, Sionna update, telemetry 수신이 모두 성공해야 `live_ready`를 출력한다.
7. `Ctrl+C`, `TERM`, 오류 또는 정상 종료 시 모든 자식 프로세스를 정리한다.

### `run-ocudu-legacy-1x1-inner.sh`

격리된 user/network/mount namespace 내부의 실제 프로세스를 관리한다.

- `ogstun`과 UE용 network namespace를 만든다.
- MongoDB와 Open5GS 5GC를 시작한다.
- CUDA Channel Broker를 시작한다.
- Sionna RT bridge를 시작한다.
- OCUDU gNB와 srsUE를 시작한다.
- 종료 시 각 프로세스를 독립 process group 단위로 정리한다.
- 임시 network namespace, TUN 장치와 mount를 제거한다.

## 5. 각 실행 구성요소의 기능

### OCUDU gNB

5G 기지국 역할을 한다. ZMQ 기반 `cf32` IQ 신호를 CUDA Channel Broker와 주고받고, Open5GS의 AMF에 연결하여 UE의 RRC 및 NAS 절차를 처리한다.

### srsUE

소프트웨어 UE 역할을 한다. Broker를 통해 gNB와 IQ 신호를 교환하고 다음 절차를 수행한다.

- 셀 탐색 및 동기화
- RRC 연결
- 5GC 등록
- PDU session 생성
- `tun_srsue`를 통한 데이터 통신

정상 실행에서는 UE가 `10.45.1.1`로 ping을 보내 데이터 경로를 검증한다.

### Open5GS와 MongoDB

Open5GS는 5G Core 역할을 하며 AMF, SMF, UPF 등의 기능을 제공한다. MongoDB는 가입자 정보를 저장한다. 런처가 실행 시 subscriber를 등록하고 gNB와 UE가 5GC에 연결될 수 있도록 설정한다.

### CUDA Channel Broker

gNB와 UE 사이의 ZMQ IQ 경로에 위치하는 실시간 채널 에뮬레이터다.

```text
gNB TX ──▶ Broker ──▶ UE RX
gNB RX ◀── Broker ◀── UE TX
```

Broker는 다음 채널 효과를 GPU에서 적용할 수 있다.

- path loss와 scalar gain
- phase
- delay 및 multipath tap
- CFO
- AWGN
- Doppler 및 Rician 성분
- 여러 입력 신호의 superposition

Broker는 각 IQ slot의 처리 시간, deadline, backend 적용 상태와 채널별 통계를 telemetry로 발행한다.

### Sionna RT bridge

Sionna RT를 이용해 장면, 송수신기 위치와 전파 경로를 계산한다. 계산된 ray를 Broker가 사용할 tap 기반 채널 프로파일로 변환한 뒤 control endpoint를 통해 원자적으로 갱신한다.

기본 update rate는 2 Hz이며 `OCUDU_NATIVE_SIONNA_UPDATE_HZ`로 변경할 수 있다.

### Web UI

Web UI는 읽기 전용 관측 화면이다. Broker를 직접 제어하지 않고 다음 두 입력을 표시한다.

- Sionna JSONL 상태 feed
- Broker telemetry feed

주요 화면 항목은 다음과 같다.

- gNB와 UE 위치 및 이동 상태
- Sionna iteration과 채널 생성 시간
- control ACK 및 backend 적용 여부
- GPU, CPU, RAM, VRAM과 PCIe 상태
- IQ slot 처리 시간과 1 ms deadline
- 링크별 path power
- 링크별 impulse response와 tap 상세 정보

Impulse response 그래프의 X축은 더 이상 항상 `0 samples`에서 시작하지 않는다. 가장 이른 tap과 가장 늦은 tap을 기준으로 여백과 눈금 간격을 자동 계산하므로, tap이 특정 구간에 모여 있어도 그래프 전체 폭을 효율적으로 사용한다.

예를 들어 tap이 `8.188–9.063 samples`에 있으면 축은 대략 다음과 같이 표시된다.

```text
8.0 samples ───────────────────────── 9.2 samples
```

## 6. 준비 완료 판정

다음 조건이 모두 충족된 후에만 `native_sionna_1x1_live_ready`가 출력된다.

1. OCUDU gNB가 정상적으로 시작됨
2. srsUE가 `RRC Connected` 상태에 도달함
3. `PDU Session Establishment successful`이 확인됨
4. UE namespace에서 `10.45.1.1` ping이 성공함
5. Sionna가 downlink와 uplink 채널 프로파일을 갱신함
6. Broker가 프로파일을 CUDA backend에 적용함
7. Web UI가 Sionna와 Broker telemetry feed를 모두 수신함

## 7. `Ctrl+C` 종료와 lock 처리

런처는 동시에 두 개의 native 1×1 실행이 시작되지 않도록 `flock`을 사용한다.

과거에는 lock file descriptor가 gNB, UE, Open5GS 같은 자식 프로세스에 상속되고 `unshare --kill-child`가 내부 런처를 즉시 종료해 cleanup trap이 실행되지 않을 수 있었다. 그러면 화면에서는 종료된 것처럼 보여도 고아 프로세스가 lock을 계속 유지하여 다음 실행에서 아래 오류가 발생했다.

```text
error: another native 1x1 run is active
```

현재 구현은 다음 방식으로 이 문제를 방지한다.

- lock descriptor는 최상위 supervisor만 유지한다.
- 장기 실행되는 namespace runtime과 Web UI에는 lock descriptor를 전달하지 않는다.
- 종료 시 `unshare` supervisor보다 내부 runner에 먼저 `TERM`을 보낸다.
- 내부 runner의 cleanup trap이 모든 process group을 순서대로 종료한다.
- cleanup 완료를 기다린 후 최상위 supervisor가 종료된다.

정상적인 `Ctrl+C` 종료 후 기대 상태는 다음과 같다.

```text
remaining_processes=0
native_lock=free
web_port_8080=closed
```

## 8. 결과 파일

Sionna 실행 로그:

```text
/home/ubuntu/ocudu-native-workspace/results/logs/ocudu-sionna-1x1/<UTC timestamp>/
```

Sionna 실행 보고서:

```text
/home/ubuntu/ocudu-native-workspace/results/reports/ocudu-sionna-1x1/<UTC timestamp>/
```

주요 결과 파일:

| 파일 | 내용 |
|---|---|
| `live-ready.json` | RRC, PDU session, ping 및 live-ready 상태 |
| `web-ui-status.json` | Web UI가 관측한 Sionna와 Broker feed 상태 |
| `attach-summary.json` | 종료 시점의 연결 결과와 Broker 오류 counter |
| `source-evidence.json` | 사용한 commit, 바이너리, 설정 파일의 SHA-256 증거 |
| `sionna-status.jsonl` | 시간에 따라 갱신되는 Sionna 계산 및 채널 상태 |

## 9. 실행 전제조건 확인

호스트는 unprivileged user namespace와 TUN 장치를 제공해야 한다.

```bash
unshare --user --map-root-user --net --mount --fork /bin/true
test -c /dev/net/tun
```

두 명령이 모두 성공해야 한다. CUDA와 native workspace도 다음 명령으로 검증할 수 있다.

```bash
cd /home/ubuntu/OCUDU/ocudu-gpu-channel

export OCUDU_NATIVE_ROOT=/home/ubuntu/ocudu-native-workspace

./scripts/native/bootstrap-workspace.sh \
  --verify-only \
  --root "$OCUDU_NATIVE_ROOT"
```

정상 검증 메시지:

```text
lock_validation=ok debs=89 archives=3 git_sources=7
claim_boundary=hermetic or arbitrary-clean-host offline build
```

이 메시지는 gNB–UE 연결 완료가 아니라, 고정된 native workspace와 의존성 검증이 통과했다는 의미다. 실제 연결 완료 여부는 `event=native_sionna_1x1_live_ready`로 판단한다.

## 10. 자주 발생하는 문제

### `another native 1x1 run is active`

현재 버전에서 정상적으로 `Ctrl+C`를 눌렀다면 lock과 자식 프로세스가 함께 정리된다. 터미널이나 런처가 `SIGKILL`로 강제 종료된 경우에는 먼저 실제 프로세스가 남았는지 확인한다.

```bash
fuser scripts/native/run-ocudu-legacy-1x1.sh 2>/dev/null || true
flock -n scripts/native/run-ocudu-legacy-1x1.sh -c 'echo native_lock=free'
```

### Web UI에 접속할 수 없음

- 런처가 `native_sionna_1x1_live_ready`를 출력했는지 확인한다.
- 서버 안에서 접속한다면 `http://127.0.0.1:8080`을 사용한다.
- 원격 PC에서는 SSH `-L` 포트 포워딩을 사용한다.
- `OCUDU_NATIVE_WEB_PORT`와 SSH 포워딩 포트가 같은지 확인한다.

### `CUDA hardware probe failed`

```bash
nvidia-smi
/opt/conda/envs/torch/bin/nvcc --version
```

GPU 번호가 다르면 `OCUDU_NATIVE_GPU_DEVICE` 값을 변경한다.

### `/dev/net/tun is absent`

호스트, VM 또는 LXC 설정에서 `/dev/net/tun`을 실행 환경에 노출해야 한다. TUN 장치가 없으면 UE 데이터 인터페이스를 만들 수 없다.

### `unshare: Operation not permitted`

호스트나 상위 컨테이너가 unprivileged user namespace 생성을 차단한 상태다. 상위 호스트 보안 정책에서 user/network/mount namespace 사용을 허용해야 한다.
