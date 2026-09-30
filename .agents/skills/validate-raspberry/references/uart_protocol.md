# Raspberry Pi ↔ Pico W UART 프로토콜 명세

## 1. 목적

이 문서는 `validate-raspberry` Skill이 Raspberry Pi와 Raspberry Pi Pico W 사이의 UART 통신을 검증할 때 사용하는 기준을 정의한다.

검증 대상은 다음과 같다.

- 모터 제어 명령
- 좌·우 바퀴 독립 속도 명령
- 정지 및 failsafe 동작
- encoder 초기화 및 telemetry
- Pico W 재부팅 감지
- UART 연결 상태 확인
- Pi와 Pico 사이 command/response 호환성

현재 전체 UART 프로토콜의 기준 구현은 다음 두 파일이다.

```text
raspberry/controllers/motor_controller.py
raspberry/pico_w_sdk/main.c
```

---

## 2. 물리 UART 구성

### Raspberry Pi 기본 장치

```text
/dev/serial0
```

### 배선

```text
Raspberry Pi GPIO14 / TXD  -> Pico W GP1 / UART0 RX
Raspberry Pi GPIO15 / RXD  <- Pico W GP0 / UART0 TX
Raspberry Pi GND           <-> Pico W GND
```

Pi와 Pico W는 반드시 공통 GND를 사용해야 한다.

### UART 설정

```text
baudrate: 115200
data bits: 8
parity: none
stop bits: 1
flow control: none
```

표준 표기:

```text
115200 8N1
```

Pi와 Pico의 baudrate가 다르면 검증 실패다.

---

## 3. 메시지 framing

UART protocol은 ASCII 기반 line protocol이다.

Pi에서 Pico로 전송:

```text
<COMMAND>\n
```

Pico에서 Pi로 전송:

```text
<RESPONSE>\n
```

현재 Pico SDK는 입력 delimiter로 `\n`과 `\r`을 모두 처리할 수 있지만, Raspberry Pi 구현은 `\n`을 사용한다.

각 필드는 comma(`,`)로 구분한다.

예:

```text
MOVE,forward,0.350
```

현재 protocol에서는 다음을 사용하지 않는다.

```text
binary packet
checksum
JSON
length prefix
```

---

## 4. UART 소유권

`/dev/serial0`은 동시에 하나의 process만 읽는 것을 원칙으로 한다.

정상 실행 구조:

```text
raspberry/robot_command_client.py
    -> MotorController
        -> /dev/serial0
```

`MotorController`는 다음 구조를 사용한다.

```text
command lock
UART write lock
단일 UART reader thread
command response queue
```

`robot_command_client.py`가 실행 중인 상태에서 다른 process가 `/dev/serial0`을 동시에 읽으면 안 된다.

두 process가 동시에 읽으면 다음 message를 서로 먼저 소비할 수 있다.

```text
OK,...
ERR,...
READY,...
EVENT,...
```

이 경우 정상 통신도 timeout이나 잘못된 응답으로 판단될 수 있다.

---

## 5. 연결 초기화 절차

`MotorController`가 UART를 새로 열 때의 기본 흐름은 다음과 같다.

```text
Pi -> Pico: ENC_STREAM,0
Pi: 기존 input buffer 정리

Pi -> Pico: PING
Pico -> Pi: OK,PONG

Pi -> Pico: STOP,pi_connected
Pico -> Pi: OK,STOP

Pi -> Pico: ENC_STREAM,1
Pico -> Pi: OK,ENC_STREAM,1
```

초기화 완료 후 기대 상태:

```text
motor.connected = true
encoder stream = enabled
current motion = stop
```

초기화 과정에서 command timeout 또는 UART 오류가 발생하면:

```text
현재 serial 연결 종료
connected 상태 해제
후속 제어 전 재연결 필요
```

---

## 6. Pico boot 및 reboot 메시지

현재 Pico SDK firmware는 초기화 완료 후 다음 메시지를 보낸다.

```text
READY,PICO_W_MOTOR_ENCODER
```

Pi는 다음 prefix를 가진 모든 메시지를 Pico reboot로 처리한다.

```text
READY,
```

Pico reboot 감지 시 Pi는 다음 상태를 적용한다.

```text
current_motion = stop
Pico reboot pending 상태 설정
```

이후 복구 과정:

```text
STOP,pico_reboot_recovery
ENC_STREAM,1
```

복구 성공 후 encoder stream을 다시 사용한다.

---

## 7. 전체 command 목록

현재 Pico SDK 기준 protocol:

| 방향 | Command / Response |
|---|---|
| Pi → Pico | `PING` |
| Pi → Pico | `STOP,<reason>` |
| Pi → Pico | `MOVE,<direction>,<speed>` |
| Pi → Pico | `DRIVE,<left>,<right>` |
| Pi → Pico | `ENC_GET` |
| Pi → Pico | `ENC_RESET` |
| Pi → Pico | `ENC_STREAM,<enabled>` |
| Pi → Pico | `LED,<enabled>` |
| Pi → Pico | `BEEP,<duration_ms>` |
| Pico → Pi | `READY,...` |
| Pico → Pi | `OK,...` |
| Pico → Pi | `ERR,...` |
| Pico → Pi | `EVENT,FAILSAFE_STOP` |
| Pico → Pi | `EVENT,ENC,...` |

---

## 8. PING

### 요청

```text
PING
```

### 정상 응답

```text
OK,PONG
```

### 검증 의미

정상 `PING` 응답은 다음 전체 경로가 동작한다는 의미다.

```text
Pi TX
-> Pico RX
-> Pico command parser
-> Pico TX
-> Pi RX
-> Pi UART reader
```

GPIO level loopback 테스트만 성공하고 `PING`이 실패하면 실제 Pi-Pico protocol 통신은 성공한 것으로 판단하지 않는다.

---

## 9. STOP

### 요청 형식

```text
STOP,<reason>
```

예:

```text
STOP,manual_stop
STOP,pi_connected
STOP,pico_reboot_recovery
STOP,websocket_disconnected
```

Pi는 reason을 UART 전송 전에 정리한다.

허용 문자:

```text
A-Z
a-z
0-9
_
-
```

최대 길이:

```text
48 characters
```

### 현재 Pico SDK 응답

```text
OK,STOP
```

현재 C firmware는 전달된 reason을 response에 다시 포함하지 않는다.

따라서 validator는 다음 응답을 요구하면 안 된다.

```text
OK,STOP,<reason>
```

### STOP 후 필수 상태

```text
left PWM = 0
right PWM = 0
is_moving = false
```

---

## 10. MOVE

### 요청 형식

```text
MOVE,<direction>,<speed>
```

예:

```text
MOVE,forward,0.350
MOVE,rotate_left,0.500
```

### 허용 direction

```text
forward
backward
left
right
forward_left
forward_right
backward_left
backward_right
rotate_left
rotate_right
```

### speed 범위

Pi 측 논리 범위:

```text
0.0 <= speed <= 1.0
```

Pi는 speed를 이 범위로 clamp한 뒤 소수점 셋째 자리 형식으로 전송한다.

예:

```text
MOVE,forward,0.350
```

### 정상 응답

```text
OK,MOVE,<direction>
```

예:

```text
OK,MOVE,forward
```

현재 Pico SDK는 response에 speed를 포함하지 않는다.

따라서 validator는 다음 형태를 필수로 요구하지 않는다.

```text
OK,MOVE,forward,0.350
```

### speed가 0 이하인 경우

Pico는 정지 상태로 전환한다.

응답:

```text
OK,STOP
```

### 잘못된 direction

예상 응답:

```text
ERR,invalid direction
```

잘못된 이동 명령이 들어오면 모터는 정지 상태여야 한다.

---

## 11. MOVE의 좌·우 바퀴 의미

다음 값을 정의한다.

```text
s = requested speed
inner = s * 0.35
```

현재 `CURVE_INNER_RATIO`:

```text
0.35
```

논리 mapping:

| Direction | Left | Right |
|---|---:|---:|
| `forward` | `+s` | `+s` |
| `backward` | `-s` | `-s` |
| `left` | `0` | `+s` |
| `right` | `+s` | `0` |
| `forward_left` | `+inner` | `+s` |
| `forward_right` | `+s` | `+inner` |
| `backward_left` | `-s` | `-inner` |
| `backward_right` | `-inner` | `-s` |
| `rotate_left` | `-s` | `+s` |
| `rotate_right` | `+s` | `-s` |

이 mapping은 software logical direction 기준이다.

실제 바퀴가 전진하는 물리 방향은 다음 설정과 배선 상태에 영향을 받는다.

```text
LEFT_FORWARD_DIR_LEVEL
RIGHT_FORWARD_DIR_LEVEL
motor wiring
```

따라서 protocol 논리 검증과 실제 모터 방향 검증은 별도로 수행한다.

---

## 12. DRIVE

`DRIVE`는 자율주행에서 좌·우 바퀴를 독립적으로 제어하기 위한 command다.

### Pi 내부 입력

Pi는 좌·우 목표 속도를 m/s 단위로 받는다.

```text
left_mps
right_mps
```

Pi는 이를 Pico용 normalized value로 변환한다.

```text
normalized = wheel_mps / MAX_WHEEL_MPS
```

현재 기본값:

```text
MAX_WHEEL_MPS = 0.50
```

정규화 결과 중 하나의 절댓값이 `1.0`을 초과하면 좌·우 값을 같은 비율로 줄여 최대 절댓값을 `1.0`으로 맞춘다.

### UART 요청

```text
DRIVE,<left_normalized>,<right_normalized>
```

예:

```text
DRIVE,0.500,0.500
DRIVE,-0.400,0.400
DRIVE,1.000,0.250
```

### 허용 범위

```text
-1.0 <= left <= 1.0
-1.0 <= right <= 1.0
```

다음 값은 허용하지 않는다.

```text
NaN
Infinity
-Infinity
```

### 정상 응답

```text
OK,DRIVE
```

### argument 누락

```text
ERR,DRIVE requires left and right
```

### 숫자 변환 실패

```text
ERR,invalid DRIVE speed
```

### 범위 초과

```text
ERR,DRIVE speed out of range
```

잘못된 `DRIVE` command가 들어오면 모터는 정지해야 한다.

---

## 13. Encoder 구성

현재 Pico SDK는 4개 모터의 quadrature encoder를 처리한다.

필드 의미:

```text
LF = Left Front
RF = Right Front
LR = Left Rear
RR = Right Rear
```

각 tick 값은 signed integer다.

현재 encoder sign 설정:

```text
LEFT_FRONT_ENCODER_SIGN  =  1
RIGHT_FRONT_ENCODER_SIGN = -1
LEFT_REAR_ENCODER_SIGN   =  1
RIGHT_REAR_ENCODER_SIGN  = -1
```

이 값은 실제 전진 방향에서 tick 부호를 맞추기 위한 calibration 값이다.

정적 validator만으로 실제 encoder sign이 맞는지는 확정할 수 없다.

---

## 14. ENC_GET

### 요청

```text
ENC_GET
```

### 정상 응답

```text
OK,ENC,<LF>,<RF>,<LR>,<RR>,<pico_ms>
```

예:

```text
OK,ENC,120,118,121,119,34852
```

### 필드

| Field | Type | 의미 |
|---|---|---|
| `LF` | signed integer | 왼쪽 앞 모터 누적 tick |
| `RF` | signed integer | 오른쪽 앞 모터 누적 tick |
| `LR` | signed integer | 왼쪽 뒤 모터 누적 tick |
| `RR` | signed integer | 오른쪽 뒤 모터 누적 tick |
| `pico_ms` | non-negative integer | Pico 부팅 이후 경과 ms |

응답 필드 개수가 다르면 protocol mismatch다.

---

## 15. ENC_RESET

### 요청

```text
ENC_RESET
```

### 응답

```text
OK,ENC_RESET
```

### 기대 상태

```text
LF = 0
RF = 0
LR = 0
RR = 0
```

Pi의 `MotorController`도 정상 reset 이후 local encoder snapshot을 초기화한다.

---

## 16. ENC_STREAM

### 활성화

요청:

```text
ENC_STREAM,1
```

응답:

```text
OK,ENC_STREAM,1
```

### 비활성화

요청:

```text
ENC_STREAM,0
```

응답:

```text
OK,ENC_STREAM,0
```

현재 Pico SDK parser는 다음 값도 지원한다.

활성:

```text
1
on
ON
true
```

비활성:

```text
0
off
OFF
false
```

Pi는 일반적으로 `0`과 `1`만 사용한다.

### 잘못된 값

```text
ERR,ENC_STREAM requires 0 or 1
```

---

## 17. Encoder stream event

encoder stream 활성 상태에서는 Pico가 주기적으로 다음 event를 보낸다.

```text
EVENT,ENC,<LF>,<RF>,<LR>,<RR>,<pico_ms>
```

예:

```text
EVENT,ENC,120,118,121,119,34852
```

현재 report interval:

```text
50 ms
```

명목상 frequency:

```text
20 Hz
```

`EVENT,ENC,...`는 비동기 telemetry다.

따라서 다음 command의 response로 소비되면 안 된다.

```text
PING
MOVE
DRIVE
STOP
ENC_RESET
ENC_STREAM
```

Pi의 UART reader는 encoder event를 별도로 처리한다.

Pi는 정상적으로 파싱된 encoder event마다 local `sequence` 값을 증가시킨다.

---

## 18. Pico firmware failsafe

Pico firmware는 Pi와 독립적인 motor timeout을 가진다.

현재 값:

```text
COMMAND_TIMEOUT_MS = 350
```

모터가 움직이는 상태에서 새로운 movement command가 timeout 이내에 도착하지 않으면:

```text
left PWM = 0
right PWM = 0
is_moving = false
```

그리고 다음 event를 전송한다.

```text
EVENT,FAILSAFE_STOP
```

Pi가 이 event를 받으면 local motion 상태도 다음으로 변경해야 한다.

```text
stop
```

Pico failsafe가 제거되거나 동작하지 않으면 안전 검증 실패다.

---

## 19. Pi software failsafe

Raspberry Pi에도 별도 timeout이 존재한다.

현재 기본값:

```text
COMMAND_TIMEOUT_SEC = 0.45
```

`robot_command_client.py`의 failsafe loop는 반복적으로 다음을 호출한다.

```text
MotorController.failsafe_tick()
```

따라서 현재 안전 구조는 두 단계다.

```text
1. Pico firmware timeout
2. Pi software timeout
```

두 기능 모두 유지되어야 한다.

Pico timeout이 Pi timeout보다 먼저 동작하는 현재 구조는 정상이다.

---

## 20. UART response 분류 순서

Pi UART reader는 수신 line을 종류별로 분리한다.

### Encoder telemetry

```text
EVENT,ENC,...
```

encoder 상태 업데이트에 사용한다.

### Pico failsafe

```text
EVENT,FAILSAFE_STOP
```

motor 상태를 stop으로 변경한다.

### Pico reboot

```text
READY,...
```

Pico reboot pending 상태를 설정한다.

### Command response

```text
OK,...
ERR,...
```

response queue에 넣는다.

### Unknown line

나머지 문자열은 diagnostic message로 처리한다.

Unknown line을 정상 command response로 처리하면 안 된다.

---

## 21. ERR 및 timeout 처리

Pico가 다음을 반환하면 command는 실패다.

```text
ERR,...
```

Pi가 지정된 serial timeout 안에 `OK` 또는 `ERR` response를 받지 못하면:

```text
현재 serial 연결 종료
connected = false
UART timeout error 발생
다음 command 전에 재연결 필요
```

현재 기본 serial response timeout:

```text
0.25 s
```

timeout 이후 이전 serial connection을 그대로 계속 사용하는 것은 정상 동작이 아니다.

---

## 22. Pico UART receive buffer

현재 Pico SDK receive buffer 크기:

```text
128 bytes
```

buffer overflow 발생 시:

```text
ERR,receive buffer overflow
```

동시에 motor를 정지한다.

정상 protocol command는 이 길이보다 충분히 짧아야 한다.

---

## 23. Pico firmware 기준

현재 배포 및 검증 대상 Pico firmware는 다음 하나입니다.

```text
raspberry/pico_w_sdk/main.c
```

Pi ↔ Pico UART contract는 위 SDK firmware와
`raspberry/controllers/motor_controller.py`를 기준으로 검증합니다.

---

## 24. Speaker 및 기타 출력

WebSocket의 `speak` command는 기존대로 Raspberry Pi의
`SpeakerController`가 처리한다. 이 경로는 이번 정리에서 변경하지 않는다.

Pico UART에는 별도로 다음 출력 command가 있다.

```text
LED,0|1
BEEP,<duration_ms>
```

`LED`는 GPIO20 경고 LED/MOSFET 출력을 지속 ON/OFF하고,
`BEEP`은 GPIO16 speaker PWM을 지정 시간 동안 재생한다.
현재 BEEP 허용 범위는 50~2000 ms이며 firmware main loop를 block하지 않는다.

`speak`는 Pico UART의 `SPEAK` command로 변환되지 않는다.

---

## 25. 정적 검증 기준

`validate_uart_protocol.py`는 가능한 범위에서 다음 항목을 검사해야 한다.

```text
Pi/Pico UART baudrate
Pi/Pico TX/RX pin contract
ASCII line protocol
PING
STOP
MOVE
DRIVE
ENC_GET
ENC_RESET
ENC_STREAM
LED
BEEP
READY 처리
EVENT,FAILSAFE_STOP
EVENT,ENC
허용 direction 목록
DRIVE 범위
encoder event field 수
encoder stream 주기
Pico failsafe timeout
Pi command timeout
```

---

## 26. 실제 hardware runtime 검증

실제 Raspberry Pi와 Pico W가 연결된 환경에서는 다음 검사를 수행할 수 있다.

### UART 연결 확인

```text
PING
```

기대:

```text
OK,PONG
```

### 안전 정지 확인

```text
STOP,test
```

기대:

```text
OK,STOP
```

이 검사는 모터를 움직이지 않고 수행할 수 있다.

### Encoder stream

```text
ENC_STREAM,1
```

기대:

```text
OK,ENC_STREAM,1
EVENT,ENC,... 반복 수신
```

### Encoder reset

```text
ENC_RESET
```

기대:

```text
OK,ENC_RESET
```

바퀴가 움직이지 않은 상태라면 reset 이후 tick은 0 또는 0에 가까운 상태여야 한다.

### 잘못된 command

예:

```text
INVALID_COMMAND
```

기대:

```text
ERR,unknown command
```

모터는 정지 상태여야 한다.

---

## 27. Failsafe runtime 검증

failsafe 검사는 실제 motor output을 발생시킬 수 있으므로 기본 검증에서는 수행하지 않는다.

실행 조건:

```text
로봇 바퀴가 지면에서 분리됨
또는 안전한 시험 공간 확보
비상 전원 차단 가능
모터 wiring 확인 완료
사용자가 실제 motor test를 명시적으로 요청함
```

검증 순서:

```text
1. 낮은 speed의 non-zero MOVE 또는 DRIVE 전송
2. 추가 movement command 전송 중단
3. 약 350ms 이후 motor stop 확인
4. EVENT,FAILSAFE_STOP 수신 확인
```

이 검사는 `--failsafe-test`와 같은 명시적인 옵션 없이 자동 실행하면 안 된다.

---

## 28. Runtime 안전 조건

실제 movement test 전에 최소한 다음을 확인한다.

```text
robot secured 또는 wheels lifted
emergency power-off 가능
motor wiring 확인
Pi와 Pico common GND 확인
UART TX/RX 배선 확인
다른 UART reader process 없음
```

통신 자체만 검증할 목적이라면 다음 command를 우선한다.

```text
PING
STOP
ENC_GET
ENC_RESET
ENC_STREAM
```

UART 연결 여부를 확인하기 위해 불필요하게 motor를 움직이지 않는다.

---

## 29. Validator 판정 기준

### ERROR

다음은 ERROR로 처리한다.

```text
Pi와 Pico baudrate 불일치
TX/RX protocol 정의 불일치
PING 미지원
Pi가 DRIVE를 요구하지만 deployed firmware가 미지원
Pi가 encoder stream을 요구하지만 firmware가 미지원
encoder event field 구조 불일치
failsafe 제거 또는 비활성화
잘못된 command에서 motor stop이 보장되지 않음
UART timeout 이후 연결 상태가 안전하게 정리되지 않음
```

### WARN

다음은 WARN으로 처리할 수 있다.

```text
response text가 다르지만 Pi parser와 호환됨
encoder sign calibration이 hardware 확인 필요
정적 검사 환경에서 /dev/serial0 사용 불가
motor physical direction을 정적으로 확인할 수 없음
```

### PASS

다음 조건을 만족하면 PASS다.

```text
Pi/Pico protocol definition 호환
필수 command 존재
response parser 호환
encoder event 처리 구조 정상
failsafe 경로 존재
요청된 runtime 검사가 정상 통과
```

---

## 30. 대표 정상 통신 예시

### 초기 연결

```text
Pico -> Pi: READY,PICO_W_MOTOR_ENCODER

Pi -> Pico: PING
Pico -> Pi: OK,PONG

Pi -> Pico: STOP,pi_connected
Pico -> Pi: OK,STOP

Pi -> Pico: ENC_STREAM,1
Pico -> Pi: OK,ENC_STREAM,1

Pico -> Pi: EVENT,ENC,0,0,0,0,1234
Pico -> Pi: EVENT,ENC,0,0,0,0,1284
```

### 수동 이동

```text
Pi -> Pico: MOVE,forward,0.350
Pico -> Pi: OK,MOVE,forward

Pi -> Pico: STOP,button_release
Pico -> Pi: OK,STOP
```

### 자율주행 differential drive

```text
Pi -> Pico: DRIVE,0.400,0.300
Pico -> Pi: OK,DRIVE

Pi -> Pico: DRIVE,0.000,0.000
Pico -> Pi: OK,DRIVE
```

### Pico failsafe

```text
Pi -> Pico: MOVE,forward,0.350
Pico -> Pi: OK,MOVE,forward

<350ms 이상 movement command 없음>

Pico -> Pi: EVENT,FAILSAFE_STOP
```

### Encoder 조회

```text
Pi -> Pico: ENC_GET
Pico -> Pi: OK,ENC,152,-148,150,-149,9540
```

### Encoder reset

```text
Pi -> Pico: ENC_RESET
Pico -> Pi: OK,ENC_RESET
```

### 잘못된 command

```text
Pi -> Pico: TEST
Pico -> Pi: ERR,unknown command
```

---

## 31. 최종 기준

현재 프로젝트의 Pi ↔ Pico W UART 검증에서 source of truth는 다음과 같다.

```text
Pi:
raspberry/controllers/motor_controller.py

Pico:
raspberry/pico_w_sdk/main.c
```

`validate-raspberry`는 두 파일의 protocol 정의가 서로 호환되는지 확인하고, 실제 hardware 검증이 요청된 경우에만 `/dev/serial0`을 사용한 runtime 검사를 추가로 수행한다.
