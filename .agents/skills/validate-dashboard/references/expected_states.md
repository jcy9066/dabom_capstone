# Dashboard Expected States

## 1. 문서 목적

이 문서는 Playwright 기반 대시보드 검증에서 정상 상태, 처리 중 상태, 차단 상태와 오류 상태를 판정하기 위한 기준이다.

현재 구현의 실제 DOM과 API contract를 기준으로 하며, 화면 문구가 일부 변경되더라도 상태 전이와 안전 조건을 우선한다.

---

## 2. 페이지 및 인증 상태

| 현재 상태        | 요청                      | 기대 결과                 |
| ------------ | ----------------------- | --------------------- |
| 비로그인         | `GET /`                 | `/login` redirect     |
| 비로그인         | `GET /login`            | 로그인 화면 200            |
| 비로그인         | `GET /main`             | `/login` redirect     |
| 로그인          | `GET /`                 | `/main` redirect      |
| 로그인          | `GET /login`            | `/main` redirect      |
| 로그인          | `GET /main`             | 대시보드 200              |
| 로그인 후 logout | `POST /api/auth/logout` | session 삭제 후 `/login` |
| logout 이후    | `GET /main`             | `/login` redirect     |

비로그인 상태의 `/main`이 직접 렌더링되면 실패다.

---

## 3. CSRF 상태

### 정상 상태

```text
GET /api/auth/csrf
HTTP 200
payload.csrf_token 존재
```

CSRF 보호가 적용되어야 하는 요청:

```text
POST /api/auth/email/send
POST /api/auth/email/verify
POST /api/auth/availability
POST /api/auth/register
POST /api/auth/login
POST /api/auth/logout
POST /api/system-control/{group}/{component}/{action}
POST /api/navigation/maps/load
POST /api/navigation/maps/rename
```

필수 header:

```text
X-CSRF-Token: <token>
```

### 오류 상태

token이 없거나 session token과 다르면:

```text
HTTP 403
ok=false
보안 검증 실패 메시지
```

UI는 오류를 표시하고 다시 시도할 수 있어야 한다.

---

## 4. 로그인 화면 상태

### 핵심 selector

```text
#loginForm
#loginId
#loginPassword
#loginMessage
#loginSubmit
#openSignupBtn
```

### 입력 상태

| 상태           | 기대 UI                |
| ------------ | -------------------- |
| 로그인 ID 비어 있음 | submit 차단 또는 오류 메시지  |
| 비밀번호 비어 있음   | submit 차단 또는 오류 메시지  |
| 요청 중         | 로그인 버튼 비활성           |
| 인증 실패        | `/login` 유지, 오류 메시지  |
| 인증 성공        | `/main` 이동           |
| 요청 완료        | 로그인 버튼 재활성 또는 페이지 이동 |

비밀번호 값은 console, URL query, error text에 표시되어서는 안 된다.

---

## 5. 회원가입 상태

### 초기 상태

```text
#signupModal: 닫힘
#protectedFields: disabled
#signupSubmit: disabled
#verifyRow: hidden
```

### modal open

```text
#signupModal.open
aria-hidden="false"
focus: #signupEmail
```

### 이메일 인증 상태

| 상태          |    보호 필드 |       인증번호 영역 |    가입 버튼 |
| ----------- | -------: | ------------: | -------: |
| 이메일 미입력     | disabled |        hidden | disabled |
| 잘못된 이메일     | disabled |        hidden | disabled |
| 인증번호 발송 완료  | disabled |       visible | disabled |
| 인증번호 오류     | disabled |       visible | disabled |
| 이메일 인증 완료   |  enabled |       visible |      조건부 |
| 인증 후 이메일 변경 | disabled | hidden 또는 초기화 | disabled |

### 가입 버튼 활성 조건

다음 조건을 모두 만족해야 한다.

```text
이메일 인증 완료
인증된 이메일과 현재 이메일 동일
로그인 ID 형식 정상
로그인 ID available=true
비밀번호 형식 정상
비밀번호 확인 일치
이름 비어 있지 않음
전화번호 형식 정상
사번 형식 정상
사번 available=true
```

---

## 6. 메인 대시보드 기본 상태

### 필수 화면 영역

| 영역       | 필수 selector                                                      |
| -------- | ---------------------------------------------------------------- |
| 메뉴       | `#menuBtn`, `#sidebar`                                           |
| 시스템 상태   | `#sys-cpu-usage`, `#sys-cpu-temp`, `#sys-ram`, `#sys-internet`   |
| 카메라      | `#camera-stream`, `#no-camera-msg`, `.live-badge`                |
| LiDAR    | `#lidar-map-canvas`, `#lidar-live-badge`, `#lidar-stale-overlay` |
| 지도 제어    | `#lidarMapSelectBtn`, `#lidarMapSaveBtn`, `#minimapExpandBtn`    |
| 경고       | `#alertBox`                                                      |
| 모드       | `#mode-switch-ui`, `#label-auto`, `#label-manual`                |
| 수동 조작    | `#d-pad-area`, `.d-pad .d-btn`                                   |
| 공통 modal | `#commonModal`, `#modalTitle`, `#modalBody`                      |
| 로그아웃     | `#logoutBtn`                                                     |

필수 selector가 하나라도 없으면 해당 기능 검증은 실패로 기록한다.

---

## 7. 로봇 상태 표시

`GET /get_status`의 기대 필드:

```json
{
  "cpu_usage": "string or number",
  "cpu_temp": "string or number",
  "ram_usage": "string or number",
  "internet": "string or number",
  "mode": "auto or manual"
}
```

### 정상 갱신 주기

```text
약 1초
```

browser scheduling에 따라 정확한 millisecond 간격은 요구하지 않는다.

### 유효 mode

```text
auto
manual
```

그 외 mode는 현재 UI mode를 임의로 변경하는 근거로 사용하지 않는다.

---

## 8. 자동·수동 모드 UI

| 상태           | `#mode-switch-ui` | `#d-pad-area`  | 자동 label     | 수동 label     |
| ------------ | ----------------- | -------------- | ------------ | ------------ |
| `auto`       | `.manual` 없음      | `.disabled` 있음 | active       | inactive     |
| `manual`     | `.manual` 있음      | `.disabled` 없음 | inactive     | active       |
| mode 미확인     | 기존 상태 유지          | 조작 차단 권장       | 확정하지 않음      | 확정하지 않음      |
| mode 변경 요청 중 | 중복 요청 차단          | 입력 중지          | 서버 status 대기 | 서버 status 대기 |
| mode 요청 실패   | 기존 mode 유지        | 기존 상태 유지       | 기존 상태        | 기존 상태        |

mode command의 HTTP 성공만으로 UI 상태를 바꾸지 않는다.

UI의 최종 기준은 이후 `/get_status` 응답이다.

---

## 9. 로봇 command contract

### 공통 endpoint

```http
POST /api/robots/pi-01/command
Content-Type: application/json
```

### mode

```json
{
  "type": "mode",
  "mode": "manual"
}
```

또는:

```json
{
  "type": "mode",
  "mode": "auto"
}
```

### move

```json
{
  "type": "move",
  "direction": "<direction>",
  "speed": 0.35
}
```

### stop

```json
{
  "type": "stop",
  "reason": "<reason>"
}
```

### emergency stop

```json
{
  "type": "emergency_stop",
  "reason": "<reason>"
}
```

### speak

```json
{
  "type": "speak",
  "text": "경고합니다. 즉시 물러나십시오."
}
```

정상 응답은 최소한 다음 조건을 만족해야 한다.

```text
HTTP 2xx
payload.ok = true
```

---

## 10. 수동 방향 매핑

페이지의 최종 script load 결과를 기준으로 한다.

| 화면 입력 | 전송 direction     |
| ----- | ---------------- |
| `↑`   | `rotate_left`    |
| `↓`   | `rotate_right`   |
| `←`   | `backward`       |
| `→`   | `forward`        |
| `↖`   | `backward_left`  |
| `↗`   | `forward_left`   |
| `↙`   | `forward_right`  |
| `↘`   | `backward_right` |

`script.js`의 초기 generic mapping보다 이후 로드되는 `control_mapping.js`의 override가 우선한다.

### 키보드 매핑

| 키                 | 화면 방향 |
| ----------------- | ----- |
| `ArrowUp`, `w`    | `↑`   |
| `ArrowDown`, `s`  | `↓`   |
| `ArrowLeft`, `a`  | `←`   |
| `ArrowRight`, `d` | `→`   |
| up + left         | `↖`   |
| up + right        | `↗`   |
| down + left       | `↙`   |
| down + right      | `↘`   |

---

## 11. 수동 입력 안전 상태

### 이동 시작

```text
current mode = manual
하나 이상의 move 요청 발생
speed = 0.35
```

### 반복 입력

```text
반복 기준 약 120ms
```

browser scheduling 때문에 정확한 횟수는 검증하지 않는다.

### 입력 종료

| event           | 기대 command                             |
| --------------- | -------------------------------------- |
| pointerup       | `stop`, reason=`button_release`        |
| pointercancel   | `stop`, reason=`pointer_cancel`        |
| 마지막 keyup       | `stop`, reason=`key_release`           |
| mode 전환 전 입력 취소 | 반복 move 중단                             |
| window blur     | `emergency_stop`, reason=`window_blur` |
| page hidden     | `emergency_stop`, reason=`page_hidden` |

release 또는 emergency stop 이후 새로운 사용자 입력 없이 move 요청이 계속 발생하면 실패다.

### 자동 모드

```text
move 요청 0건
D-Pad disabled
키보드 drive 입력 무시
```

---

## 12. 카메라 상태

### LIVE

조건:

```json
{
  "camera_state": "live"
}
```

기대 상태:

```text
.live-badge text = LIVE
.live-badge.offline 없음
#camera-stream 표시
#no-camera-msg 숨김
#video-wrapper.camera-offline 없음
```

### OFFLINE

조건:

```json
{
  "camera_state": "offline",
  "message": "<message>"
}
```

기대 상태:

```text
.live-badge text = OFFLINE
.live-badge.offline 존재
#no-camera-msg 표시
응답 message 표시
#video-wrapper.camera-offline 존재
```

### 이미지 오류

기대 message:

```text
영상 스트림을 불러올 수 없습니다
```

---

## 13. LiDAR 상태

### polling 주기

| API                      |    기준 주기 |
| ------------------------ | -------: |
| `/api/navigation/status` | 약 1000ms |
| `/api/navigation/map`    | 약 1500ms |
| `/api/navigation/pose`   |  약 500ms |
| `/api/navigation/scan`   |  약 500ms |

### 상태 전이

| 조건                       | badge   | overlay title | detail               |
| ------------------------ | ------- | ------------- | -------------------- |
| map·pose·scan 모두 없음      | OFFLINE | LiDAR OFFLINE | NO SCAN DATA         |
| backend status=`offline` | OFFLINE | LiDAR OFFLINE | 마지막 수신 age           |
| age > 8초                 | OFFLINE | LiDAR OFFLINE | 마지막 수신 age           |
| backend status=`stale`   | STALE   | LiDAR STALE   | 마지막 수신 age           |
| age > 3초                 | STALE   | LiDAR STALE   | 마지막 수신 age           |
| 최신 데이터 있음                | LIVE    | overlay 숨김    | age 또는 live metadata |

### 허용 navigation mode

```text
scan_only
mapping
localization
localization_nav2
```

### 안전 필드

정상 개발·검증 상태:

```json
{
  "dry_run": true,
  "motor_output_enabled": false
}
```

`motor_output_enabled=true`가 표시되면 실제 동작이 의도된 환경인지 확인하기 전까지 안전 실패로 처리한다.

---

## 14. LiDAR canvas 상태

### map 존재

```text
canvas width > 0
canvas height > 0
map width > 0
map height > 0
resolution > 0
```

RLE map data가 decode되어 canvas에 반영되어야 한다.

### pose 존재

다음 값이 유한 숫자여야 한다.

```text
x
y
yaw
```

로봇 marker는 map coordinate를 canvas coordinate로 변환하여 표시한다.

### scan 존재

다음 값이 유효해야 한다.

```text
ranges 배열
angle_min
angle_increment
range_min
range_max
```

`null`, `NaN`, `Infinity`, range 범위 밖 값은 렌더링에서 제외한다.

---

## 15. 저장 지도 상태

### map 목록 응답

최소 구조:

```json
{
  "ok": true,
  "maps": [
    {
      "map_name": "slam_test_01",
      "saved_at": "<timestamp>",
      "width": 100,
      "height": 100,
      "resolution": 0.05
    }
  ]
}
```

### active map

```json
{
  "ok": true,
  "state": "active",
  "active_map": {
    "map_name": "slam_test_01"
  }
}
```

허용 state:

```text
active
loading
resetting_pose
verifying
unavailable
not selected
```

### 지도 modal

| 데이터 상태        | 기대 결과                      |
| ------------- | -------------------------- |
| maps 존재       | radio 목록 표시                |
| active map 존재 | ACTIVE 표시 및 기본 선택          |
| maps 없음       | 저장 지도 없음 메시지               |
| API 404       | active API unavailable 처리  |
| API 오류        | modal 오류 또는 unavailable 표시 |

### 초기 위치

```text
x: 유한 숫자
y: 유한 숫자
yaw_degrees: 유한 숫자
```

유효하지 않은 숫자는 request 전에 차단한다.

---

## 16. 지도 작업 오류 code

| error code                   | 기대 사용자 안내                  |
| ---------------------------- | -------------------------- |
| `MAP_SERVER_UNAVAILABLE`     | map server 실행 상태 확인        |
| `LOCALIZATION_NOT_ACTIVE`    | map_server와 AMCL 활성 상태 확인  |
| `MAPPING_MODE_ACTIVE`        | Mapping 모드에서 저장 지도 load 불가 |
| `MAP_VERIFICATION_FAILED`    | 지도 metadata 확인 실패          |
| `AMCL_VERIFICATION_FAILED`   | AMCL 초기 위치 확인 실패           |
| `INITIAL_POSE_OUT_OF_BOUNDS` | 초기 위치가 지도 범위 밖             |
| `MAP_LOAD_IN_PROGRESS`       | 다른 지도 load 처리 중            |
| `MAP_OPERATION_IN_PROGRESS`  | 다른 저장 지도 작업 처리 중           |
| `MAP_NAME_ALREADY_EXISTS`    | 같은 이름 존재                   |
| `MAP_NAME_UNCHANGED`         | 기존 이름과 같음                  |
| `INVALID_MAP_NAME`           | 허용되지 않는 이름                 |

오류 후 입력과 버튼은 다시 사용할 수 있어야 한다.

---

## 17. 시스템 제어 상태

### status endpoint

```text
GET /api/system-control/status
```

기준 polling 주기:

```text
약 1초
```

### component 상태

| state         | 표시    | 기본 action         | 버튼  |
| ------------- | ----- | ----------------- | --- |
| `on`          | ON    | stop              | 활성  |
| `off`         | OFF   | start             | 활성  |
| `duplicate`   | 중복    | stop 또는 normalize | 활성  |
| `unreachable` | 확인 불가 | 없음                | 비활성 |
| `error`       | 오류    | 구현 상태에 따름         | 조건부 |
| pending       | 처리 중  | 없음                | 비활성 |

### 선택 metadata

```text
component.label
component.description
component.instance_count
component.pids
component.message
component.duplicate
component.control_available
```

### action 요청

```http
POST /api/system-control/{group}/{component_id}/{action}
X-CSRF-Token: <token>
```

허용 group:

```text
gpu
pi
```

허용 action:

```text
start
stop
normalize
```

요청 완료 후 status를 다시 조회해야 한다.

---

## 18. 사이드바 상태

### 닫힘

```text
#sidebar.open 없음
#sidebarOverlay.open 없음
```

### 열림

```text
#sidebar.open 존재
#sidebarOverlay.open 존재
```

overlay 또는 close 버튼을 누르면 닫힘 상태로 복구된다.

---

## 19. 다크 모드 상태

### OFF

```text
body.dark-mode 없음
#darkToggle.active 없음
localStorage.darkMode = "0" 또는 없음
```

### ON

```text
body.dark-mode 존재
#darkToggle.active 존재
localStorage.darkMode = "1"
```

reload 이후에도 저장된 상태가 복원되어야 한다.

---

## 20. 공통 modal 상태

### 닫힘

```text
#commonModal display = none
```

### 열림

```text
#commonModal 표시
#modalTitle에 현재 기능 제목
#modalBody에 현재 기능 내용
```

modal backdrop 또는 close control을 사용하면 닫혀야 한다.

다른 modal을 열 때 이전 timer나 polling이 중복 실행되지 않아야 한다.

---

## 21. 네트워크 contract 요약

| Method | Endpoint                      | 목적                   |
| ------ | ----------------------------- | -------------------- |
| GET    | `/api/auth/csrf`              | CSRF 발급              |
| POST   | `/api/auth/login`             | 로그인                  |
| POST   | `/api/auth/logout`            | 로그아웃                 |
| POST   | `/api/auth/email/send`        | 인증번호 발송              |
| POST   | `/api/auth/email/verify`      | 인증번호 확인              |
| POST   | `/api/auth/availability`      | ID·사번 중복 확인          |
| POST   | `/api/auth/register`          | 회원가입                 |
| GET    | `/get_status`                 | 로봇·시스템 상태            |
| GET    | `/api/stream_status`          | 카메라 상태               |
| GET    | `/api/navigation/status`      | navigation 상태        |
| GET    | `/api/navigation/map`         | 최신 map               |
| GET    | `/api/navigation/pose`        | 최신 pose              |
| GET    | `/api/navigation/scan`        | 최신 scan              |
| GET    | `/api/navigation/maps`        | 저장 지도 목록             |
| GET    | `/api/navigation/maps/active` | active 지도            |
| POST   | `/api/navigation/maps/save`   | 지도 저장                |
| POST   | `/api/navigation/maps/load`   | 지도 load와 초기 위치       |
| POST   | `/api/navigation/maps/rename` | 지도 이름 변경             |
| GET    | `/api/system-control/status`  | ROS process 상태       |
| POST   | `/api/system-control/...`     | process 제어           |
| POST   | `/api/robots/pi-01/command`   | mode·move·stop·speak |
| POST   | `/send_telegram`              | Telegram 신고          |

---

## 22. 브라우저 오류 판정

### FAIL

* uncaught page error
* 핵심 same-origin JS 또는 CSS 404
* selector 누락으로 기능 실행 불가
* API 응답 실패 후 UI pending 상태가 해제되지 않음
* 잘못된 command payload
* 자동 모드에서 move 요청
* 최종 stop 또는 emergency stop 누락
* 인증 없이 main 접근 가능
* CSRF 누락
* password 또는 token console 노출
* `motor_output_enabled=true`가 의도되지 않은 환경에서 발견
* dialog 취소 후에도 요청이 발생함
* map load 실패 후 modal이 조작 불가능해짐

### WARN

* 외부 font resource 오류
* 실제 hardware 미연결로 인한 OFFLINE
* 실제 테스트 데이터 없음
* 의도적으로 만든 실패 응답
* 시스템 component `unreachable`
* 카메라 stream 미제공 상태에서 offline UI가 정상 표시됨

### PASS

다음 조건을 모두 만족한다.

```text
필수 사용자 흐름 완료
핵심 selector 존재
예상 request contract 일치
안전 차단 정상
콘솔에 예기치 않은 error 없음
같은 origin 필수 request에 예상치 못한 실패 없음
오류 상태에서 UI 복구 가능
실제 모터 또는 외부 서비스에 의도치 않은 요청 없음
```
