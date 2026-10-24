# Dashboard Validation Flows

## 1. 목적

이 문서는 `validate-dashboard` Skill이 Playwright MCP를 사용하여 다봄 웹 대시보드의 실제 사용자 흐름을 검증할 때 적용하는 기준을 정의한다.

검증 대상은 다음과 같다.

* 로그인 및 세션 권한
* 회원가입 화면 상태
* 메인 대시보드 초기 렌더링
* 시스템 상태 표시
* 카메라 연결 상태
* LiDAR 지도·위치·스캔 상태
* 저장 지도 조회·저장·불러오기·이름 변경
* 자동 순찰과 수동 순찰 전환
* D-Pad 및 키보드 수동 주행
* 정지 및 긴급 정지
* ROS 프로세스 상태·제어 UI
* 사이드바, 다크 모드, 로그아웃
* 브라우저 콘솔 오류와 네트워크 실패

이 문서는 정상 동작만 확인하는 smoke test가 아니다. 변경된 기능과 직접 연결된 오류 상태, 차단 상태, 안전 상태도 함께 확인한다.

---

## 2. 기본 실행 환경

### 2.1 서버 주소

대시보드 주소는 실행 중인 FastAPI 서버의 실제 주소를 사용한다.

기본 설정이 유지된 로컬 환경에서는 다음 주소가 사용될 수 있다.

```text
http://127.0.0.1:21063
```

서버 포트가 환경 변수 또는 실행 인자로 변경된 경우 실제 서버 로그와 설정을 우선한다.

주소를 추측하지 말고 다음 순서로 결정한다.

1. 사용자가 명시한 주소
2. 현재 실행 중인 서버 로그
3. 환경 변수의 서버 주소와 포트
4. 기본값 `http://127.0.0.1:21063`

### 2.2 브라우저 컨텍스트

각 인증 흐름은 가능한 한 독립된 browser context에서 실행한다.

* 비로그인 검증은 쿠키가 없는 새 context를 사용한다.
* 로그인 검증은 정상 계정으로 별도 context를 사용한다.
* 로그인 계정은 사용자, 테스트 fixture 또는 환경 설정에서 가져온다.
* 임의의 계정이나 비밀번호를 만들어 사용하지 않는다.
* 실제 이메일 발송이나 실제 회원가입은 명시적으로 필요한 경우에만 수행한다.

### 2.3 공통 감시 항목

페이지를 열기 전에 다음 이벤트를 수집한다.

* `console.error`
* `console.warn`
* uncaught page error
* failed request
* HTTP 4xx/5xx response
* 정적 asset 404
* 예상하지 않은 redirect
* 브라우저 dialog
* WebSocket 연결 오류

기능별로 의도적으로 발생시킨 실패 응답은 별도로 표시하며, 일반 오류 집계와 구분한다.

---

## 3. 안전 규칙

### 3.1 기본 원칙

대시보드 검증 중 실제 모터를 움직여서는 안 된다.

다음 요청은 기본적으로 Playwright route interception 또는 테스트 서버를 사용해 대체한다.

```text
POST /api/robots/pi-01/command
POST /send_telegram
POST /api/system-control/{group}/{component_id}/{action}
POST /api/navigation/maps/save
POST /api/navigation/maps/load
POST /api/navigation/maps/rename
```

실제 환경에 요청을 전달할 수 있는 것은 다음 조건을 모두 만족할 때뿐이다.

* 사용자가 실제 통합 검사를 명시적으로 요청했다.
* 로봇이 바닥에서 분리되었거나 안전한 시험 공간에 있다.
* 비상 정지 수단이 준비되어 있다.
* 모터 출력 활성 여부가 확인되었다.
* 실행할 요청과 예상 동작이 사전에 명확하다.

### 3.2 절대 변경하지 않는 값

검증을 위해 다음 값을 활성화하거나 우회하지 않는다.

```text
MOTOR_OUTPUT_ENABLED
motor_output_enabled
dry_run
```

서버가 `motor_output_enabled=false`를 반환하면 정상 안전 상태로 간주한다.

### 3.3 수동 주행 요청 검증

실제 주행 대신 `/api/robots/pi-01/command`를 interception하고 다음을 기록한다.

* method
* URL
* JSON payload
* 요청 횟수
* 요청 순서
* 마지막 요청
* 버튼 또는 키 해제 이후 추가 이동 요청 발생 여부

---

## 4. 인증 및 페이지 권한 흐름

### Flow A-1. 비로그인 첫 접속

1. 쿠키가 없는 새 browser context를 연다.
2. `/`에 접속한다.
3. 최종 URL을 확인한다.
4. 로그인 화면의 핵심 요소를 확인한다.

필수 결과:

```text
최종 URL: /login
#loginForm 표시
#loginId 표시
#loginPassword 표시
#loginSubmit 표시
#openSignupBtn 표시
```

### Flow A-2. 비로그인 `/main` 접근 차단

1. 쿠키가 없는 상태에서 `/main`에 직접 접속한다.
2. 최종 URL과 HTTP redirect chain을 확인한다.

필수 결과:

```text
/main → /login
```

비로그인 상태에서 `/main`이 HTTP 200으로 표시되면 권한 검증 실패다.

서버에 동일 경로가 중복 등록되어 있더라도 이를 정상으로 간주하지 않는다. 인증이 적용된 동작을 요구사항으로 사용한다.

### Flow A-3. 로그인 실패

1. 로그인 ID와 비밀번호를 입력한다.
2. 로그인 요청을 실패 응답으로 interception하거나 실제 테스트 계정에 잘못된 비밀번호를 사용한다.
3. form을 제출한다.

필수 결과:

* 현재 페이지가 `/login`에 유지된다.
* `#loginMessage`에 오류 메시지가 표시된다.
* 로그인 버튼이 영구적으로 비활성화되지 않는다.
* 브라우저에 uncaught error가 발생하지 않는다.
* 비밀번호가 URL, console 또는 DOM 외부에 노출되지 않는다.

### Flow A-4. 로그인 성공

1. GET `/api/auth/csrf`가 성공하는지 확인한다.
2. 로그인 ID와 비밀번호를 입력한다.
3. POST `/api/auth/login`의 request header와 body를 확인한다.
4. 정상 응답 이후 이동을 확인한다.

필수 요청:

```http
POST /api/auth/login
Content-Type: application/json
X-CSRF-Token: <발급된 토큰>
```

필수 body 구조:

```json
{
  "login_id": "<login id>",
  "password": "<password>"
}
```

필수 결과:

```text
최종 URL: /main
```

로그인 성공 후 `/login`에 접속했을 때 `/main`으로 이동해야 한다.

---

## 5. 회원가입 화면 흐름

실제 이메일 전송이 검증 범위가 아니라면 관련 API를 interception한다.

### Flow B-1. 회원가입 modal

1. `#openSignupBtn`을 누른다.
2. `#signupModal`이 열린 상태인지 확인한다.
3. Escape와 backdrop 클릭으로 닫히는지 확인한다.

초기 필수 상태:

* `#signupEmail` 활성
* `#sendEmailBtn` 활성
* `#protectedFields` 비활성
* `#signupSubmit` 비활성
* 인증 전 로그인 ID·비밀번호·이름·전화번호·사번 입력 차단

### Flow B-2. 이메일 입력 검증

다음 입력을 각각 검사한다.

```text
빈 문자열
@가 없는 문자열
도메인이 없는 문자열
정상 이메일
```

잘못된 이메일에서는 API 요청이 발생하지 않아야 한다.

정상 이메일에서는 다음 요청이 발생해야 한다.

```http
POST /api/auth/email/send
X-CSRF-Token: <token>
```

정상 응답 후:

* `#verifyRow` 표시
* `#verificationCode` 활성
* 재발송 제한 상태 표시
* 인증번호는 6자리 숫자로 제한

### Flow B-3. 이메일 인증 완료

다음 요청을 확인한다.

```http
POST /api/auth/email/verify
```

정상 응답 후:

* `#protectedFields` 활성
* 인증 완료 이메일 표시
* 가입 정보 입력 가능
* 인증된 이메일을 변경하면 인증 상태 초기화

### Flow B-4. 가입 정보 검증

검증 기준:

```text
로그인 ID:
- 영문 포함
- 영문·숫자만 사용
- 4~20자

비밀번호:
- 6~20자
- 영문, 숫자, !, @, #, $ 사용 가능

사번:
- 숫자만 사용
- 최대 10자리
- signed 32-bit integer 범위 이내

인증번호:
- 숫자 6자리
```

로그인 ID와 사번은 `/api/auth/availability` 결과가 `available=true`일 때만 가입 가능해야 한다.

비밀번호와 비밀번호 확인이 다르면 가입 버튼이 활성화되어서는 안 된다.

---

## 6. 메인 대시보드 초기 상태

### Flow C-1. 핵심 DOM 확인

로그인 후 `/main`에서 다음 요소가 존재해야 한다.

```text
#menuBtn
#sidebar
#logoutBtn

#sys-cpu-usage
#sys-cpu-temp
#sys-ram
#sys-internet

#camera-stream
#no-camera-msg
.live-badge

#lidar-map-canvas
#lidar-live-badge
#lidar-map-status
#lidar-map-meta
#lidar-stale-overlay
#lidarMapSelectBtn
#lidarMapSaveBtn
#minimapExpandBtn

#alertBox

#mode-switch-ui
#label-auto
#label-manual
#d-pad-area
.d-pad .d-btn

#commonModal
#modalTitle
#modalBody
```

### Flow C-2. 정적 asset 확인

다음 resource가 404 없이 로드되어야 한다.

```text
/static/style.css
/static/system_control.css
/static/navigation_map_control.css
/static/script.js
/static/system_control.js
/static/navigation_map_control.js
/static/control_mapping.js
```

Google Fonts와 같은 외부 resource 실패는 네트워크 환경과 UI 영향 여부를 함께 보고한다. 같은 origin의 필수 asset 실패는 검증 실패다.

---

## 7. 로봇 상태 폴링

### Flow D-1. 자동 모드 상태

`GET /get_status`를 다음처럼 interception한다.

```json
{
  "cpu_usage": "15.2",
  "cpu_temp": "48.1",
  "ram_usage": "32.4",
  "internet": "ok",
  "mode": "auto"
}
```

필수 결과:

* CPU, 온도, RAM, 인터넷 표시 갱신
* 자동 순찰 label 활성
* 수동 순찰 label 비활성
* `#d-pad-area`에 `disabled` class 존재
* `#mode-switch-ui`에 `manual` class 없음

### Flow D-2. 수동 모드 상태

다음 status를 반환한다.

```json
{
  "cpu_usage": "15.2",
  "cpu_temp": "48.1",
  "ram_usage": "32.4",
  "internet": "ok",
  "mode": "manual"
}
```

필수 결과:

* 수동 순찰 label 활성
* 자동 순찰 label 비활성
* `#d-pad-area`에서 `disabled` class 제거
* `#mode-switch-ui`에 `manual` class 존재

### Flow D-3. 상태 API 실패

`GET /get_status`가 500 또는 network error를 반환하도록 한다.

필수 결과:

* 페이지 전체가 중단되지 않는다.
* uncaught page error가 발생하지 않는다.
* console에 오류가 기록될 수 있다.
* 마지막 정상 UI 상태가 임의로 반대 모드로 바뀌지 않는다.

---

## 8. 카메라 흐름

### Flow E-1. 카메라 LIVE

`GET /api/stream_status`가 다음을 반환하도록 한다.

```json
{
  "camera_state": "live",
  "message": "camera live"
}
```

필수 결과:

* `.live-badge` text가 `LIVE`
* `.live-badge`에 `offline` class 없음
* `#camera-stream` 표시
* `#no-camera-msg` 숨김
* `#video-wrapper`에 `camera-offline` class 없음

### Flow E-2. 카메라 OFFLINE

다음 응답을 사용한다.

```json
{
  "camera_state": "offline",
  "message": "카메라 신호 대기 중"
}
```

필수 결과:

* `.live-badge` text가 `OFFLINE`
* `.live-badge`에 `offline` class 존재
* `#no-camera-msg` 표시
* 응답의 message 표시
* 카메라 stream이 숨겨지거나 offline 상태로 표시

### Flow E-3. stream resource 오류

`#camera-stream`의 image 요청을 실패시킨다.

필수 결과:

```text
영상 스트림을 불러올 수 없습니다
```

페이지 전체가 중단되어서는 안 된다.

---

## 9. LiDAR 흐름

다음 API를 독립적으로 interception한다.

```text
GET /api/navigation/status
GET /api/navigation/map
GET /api/navigation/pose
GET /api/navigation/scan
```

### Flow F-1. 데이터 없음

모든 데이터 API가 빈 상태를 반환하도록 한다.

필수 결과:

```text
#lidar-live-badge: OFFLINE
#lidar-stale-title: LiDAR OFFLINE
#lidar-stale-detail: NO SCAN DATA
```

### Flow F-2. LIVE

status 예시:

```json
{
  "status": "mapping",
  "last_update_age_sec": 0.2,
  "has_map": true,
  "has_pose": true,
  "has_scan": true,
  "dry_run": true,
  "motor_output_enabled": false
}
```

필수 결과:

* badge가 `LIVE`
* stale overlay가 숨겨짐
* canvas의 width와 height가 0보다 큼
* map data가 있으면 occupancy map이 렌더링됨
* pose가 있으면 로봇 위치 marker가 렌더링됨
* scan이 있으면 scan point가 렌더링됨
* 안전 상태가 `dry_run=true`, `motor_output_enabled=false`

### Flow F-3. STALE

다음 중 하나를 사용한다.

```text
status = stale
last_update_age_sec > 3
```

필수 결과:

```text
badge: STALE
title: LiDAR STALE
detail에 마지막 update age 표시
```

### Flow F-4. OFFLINE

다음 중 하나를 사용한다.

```text
status = offline
last_update_age_sec > 8
```

필수 결과:

```text
badge: OFFLINE
title: LiDAR OFFLINE
```

### Flow F-5. 미니맵 확대·축소

1. `#minimapExpandBtn` 클릭
2. 미니맵에 `expanded` class가 추가되는지 확인
3. 버튼 text가 축소 상태 표시로 변경되는지 확인
4. 다시 클릭
5. 원래 크기로 복구되는지 확인

---

## 10. 지도 저장·선택·불러오기

### Flow G-1. map data 없이 저장

LiDAR map이 없는 상태에서 `#lidarMapSaveBtn`을 클릭한다.

필수 결과:

* 저장할 map이 없다는 alert
* `POST /api/navigation/maps/save` 요청 없음

### Flow G-2. map 저장 성공

map data가 존재하는 상태에서 저장 버튼을 누른다.

1. prompt에 map 이름 입력
2. 다음 요청을 확인한다.

```http
POST /api/navigation/maps/save
Content-Type: application/json
```

```json
{
  "map_name": "playwright_test_map"
}
```

처리 중 필수 상태:

* save 버튼 비활성
* 버튼 text가 `...`

성공 후:

* 성공 alert
* 버튼 다시 활성
* text가 `SAVE`로 복원

### Flow G-3. 저장 지도 modal

`#lidarMapSelectBtn`을 클릭한다.

필수 API:

```text
GET /api/navigation/maps
GET /api/navigation/maps/active
```

필수 상태:

* `#commonModal` 표시
* 제목이 저장 지도 선택
* `input[name="saved-navigation-map"]` 생성
* active map에는 `ACTIVE` 표시
* map 이름, 저장 시각, 크기, resolution 표시
* 초기 위치 입력 표시

```text
#saved-map-pose-x
#saved-map-pose-y
#saved-map-pose-yaw
```

### Flow G-4. 지도 불러오기

1. 지도 선택
2. X, Y, yaw 입력
3. load 버튼 클릭
4. confirmation dialog 승인
5. CSRF 요청 확인
6. load 요청 확인

필수 body:

```json
{
  "map_name": "<selected map>",
  "initial_pose": {
    "x": 0,
    "y": 0,
    "yaw_degrees": 0
  }
}
```

처리 상태는 다음 순서로 표시될 수 있다.

```text
지도 불러오는 중
AMCL 초기 위치 설정 중
지도 및 localization 확인 중
성공
```

성공 후 active map label이 갱신되어야 한다.

### Flow G-5. 잘못된 초기 위치

X, Y 또는 yaw에 유한 숫자가 아닌 값을 입력한다.

필수 결과:

* API 요청 없음
* 오류 메시지 표시
* modal 유지
* 입력과 버튼 재사용 가능

### Flow G-6. 지도 이름 변경

저장 지도 항목을 우클릭한다.

필수 요청:

```http
POST /api/navigation/maps/rename
X-CSRF-Token: <token>
Content-Type: application/json
```

```json
{
  "map_name": "<old>",
  "new_map_name": "<new>"
}
```

성공 후:

* 목록 재조회
* 새 이름 표시
* active map이었다면 active label도 변경

실패 code는 사용자용 메시지로 변환되어야 한다.

---

## 11. 자동·수동 모드 전환

### Flow H-1. 자동에서 수동으로 전환

초기 `/get_status` 응답은 `mode=auto`로 설정한다.

1. mode toggle 영역 클릭
2. confirmation dialog 승인
3. command request 확인

필수 payload:

```json
{
  "type": "mode",
  "mode": "manual"
}
```

중요한 상태 규칙:

* POST 성공만으로 UI가 즉시 manual로 바뀌어서는 안 된다.
* 이후 `/get_status`에서 `mode=manual`이 반환될 때 UI가 변경되어야 한다.

### Flow H-2. 수동에서 자동으로 전환

초기 `/get_status` 응답은 `mode=manual`로 설정한다.

필수 payload:

```json
{
  "type": "mode",
  "mode": "auto"
}
```

전환 직전 진행 중인 pointer·keyboard 입력은 종료되어야 한다.

### Flow H-3. 로봇 연결 실패

command endpoint가 실패 응답을 반환하도록 한다.

필수 결과:

* 모드가 임의로 바뀌지 않는다.
* 연결 실패 alert 표시
* pending 상태 해제
* toggle을 다시 사용할 수 있다.

---

## 12. D-Pad 수동 주행

### 12.1 최종 방향 매핑

최종 script load 이후 다음 매핑을 기대한다.

```text
↑  → rotate_left
↓  → rotate_right
←  → backward
→  → forward
↖ → backward_left
↗ → forward_left
↙ → forward_right
↘ → backward_right
```

이는 현재 실제 모터 구동 방향에 맞춘 보정 값이다.

### Flow I-1. 자동 모드에서 차단

`mode=auto` 상태에서 D-Pad 버튼 또는 방향키를 입력한다.

필수 결과:

* move command 요청 없음
* D-Pad disabled 표시 유지
* 브라우저 오류 없음

### Flow I-2. 수동 모드 버튼 입력

`mode=manual` 상태에서 각 방향 버튼을 검사한다.

필수 payload 형식:

```json
{
  "type": "move",
  "direction": "<mapped command>",
  "speed": 0.35
}
```

버튼을 누르고 있는 동안 요청이 반복될 수 있다. exact count는 browser scheduling에 따라 달라질 수 있으므로 다음만 보장한다.

* 하나 이상의 move 요청 발생
* 모든 move payload가 같은 direction
* release 이후 최종 stop 요청 발생
* release 이후 추가 move 요청 없음

### Flow I-3. 키보드 입력

다음 키를 검사한다.

```text
ArrowUp / w
ArrowDown / s
ArrowLeft / a
ArrowRight / d
```

조합 입력:

```text
up + left
up + right
down + left
down + right
```

필수 결과:

* 해당 D-Pad 방향과 같은 command 생성
* 현재 방향 버튼에 active 표시
* 마지막 키 해제 시 stop 요청
* 페이지 scroll과 같은 기본 방향키 동작 차단

### Flow I-4. 입력 중 모드 변경

수동 이동 입력 중 자동 모드 전환을 시도한다.

필수 결과:

* 반복 timer 종료
* local pressed-key 상태 초기화
* 모드 요청 전후에 이동 요청이 계속 발생하지 않음

---

## 13. 정지 및 긴급 정지

### Flow J-1. 버튼 해제

pointerup 시 다음 요청을 기대한다.

```json
{
  "type": "stop",
  "reason": "button_release"
}
```

### Flow J-2. pointer cancel

```json
{
  "type": "stop",
  "reason": "pointer_cancel"
}
```

### Flow J-3. 키 해제

마지막 drive key가 해제되면:

```json
{
  "type": "stop",
  "reason": "key_release"
}
```

### Flow J-4. window blur

수동 모드에서 window blur를 발생시킨다.

필수 payload:

```json
{
  "type": "emergency_stop",
  "reason": "window_blur"
}
```

### Flow J-5. page hidden

수동 모드에서 page visibility가 hidden으로 바뀌는 상황을 검사한다.

필수 payload:

```json
{
  "type": "emergency_stop",
  "reason": "page_hidden"
}
```

가능한 경우 `keepalive=true` 요청 속성을 함께 확인한다.

---

## 14. 시스템 제어 패널

### Flow K-1. 패널 생성

사이드바를 연 뒤 다음 동적 요소를 확인한다.

```text
#systemControlPanel
#gpuSystemControls
#piSystemControls
#systemControlUpdated
```

`GET /api/system-control/status`는 약 1초 간격으로 호출된다.

### Flow K-2. component 상태 렌더링

다음 상태를 각각 검사한다.

```text
on
off
duplicate
unreachable
error
```

필수 결과:

* `on`: stop 동작 제공
* `off`: start 동작 제공
* `duplicate`: duplicate 표시와 normalize 버튼 제공
* `unreachable`: 버튼 비활성
* `error`: 오류 상태 표시
* instance count와 PID가 있으면 표시
* message가 있으면 표시

### Flow K-3. start·stop·normalize 요청

실제 프로세스를 변경하지 않도록 요청을 interception한다.

필수 순서:

1. GET `/api/auth/csrf`
2. POST `/api/system-control/{group}/{component_id}/{action}`
3. 처리 중 버튼 비활성
4. 완료 후 status 재조회

필수 header:

```text
X-CSRF-Token
```

허용 action:

```text
start
stop
normalize
```

---

## 15. 신고와 경고 방송

### Flow L-1. Telegram 신고

기본 검증에서는 `/send_telegram`을 interception한다.

필수 결과:

* confirmation dialog 표시
* 승인 시 POST 요청
* 성공 응답 시 완료 alert
* 실패 응답 시 실패 alert

실제 Telegram 메시지를 전송하지 않는다.

### Flow L-2. 경고 방송

경고 버튼 클릭 시 다음 payload를 확인한다.

```json
{
  "type": "speak",
  "text": "경고합니다. 즉시 물러나십시오."
}
```

실제 스피커 출력은 기본 Playwright 검증에서 차단한다.

---

## 16. 사이드바와 사용자 설정

### Flow M-1. 사이드바

1. `#menuBtn` 클릭
2. `#sidebar`에 `open` class 확인
3. `#sidebarOverlay`에 `open` class 확인
4. overlay 또는 close 버튼 클릭
5. 두 요소에서 `open` class 제거 확인

### Flow M-2. 다크 모드

1. `#darkToggle` 클릭
2. `body.dark-mode` 확인
3. localStorage `darkMode=1` 확인
4. page reload
5. 다크 모드 복원 확인
6. 다시 끄고 `darkMode=0` 확인

---

## 17. 로그아웃

### Flow N-1. 정상 로그아웃

1. 로그인 상태에서 사이드바를 연다.
2. `#logoutBtn` 클릭
3. GET `/api/auth/csrf` 확인
4. POST `/api/auth/logout` 확인
5. 최종 URL 확인

필수 결과:

```text
최종 URL: /login
```

로그아웃 후 같은 context에서 `/main`에 직접 접근하면 다시 `/login`으로 이동해야 한다.

### Flow N-2. 로그아웃 실패

logout API를 실패시키면:

* 오류가 console에 기록됨
* 버튼이 영구 비활성화되지 않음
* 현재 인증 화면을 임의로 벗어나지 않음

---

## 18. 콘솔 및 네트워크 판정

### 실패로 판정

* uncaught JavaScript exception
* same-origin 필수 JS/CSS 404
* 인증되지 않은 `/main` 접근 허용
* CSRF가 필요한 쓰기 요청에 token 누락
* 자동 모드에서 move 요청 발생
* 버튼 또는 키 해제 후 stop 요청 누락
* blur 또는 page hidden 후 emergency stop 누락
* 수동 command가 최종 방향 매핑과 다름
* mode POST 직후 실제 status 확인 없이 UI가 변경됨
* 요청 실패 후 button이 계속 비활성화됨
* `motor_output_enabled=true`
* 실제 모터 요청이 의도치 않게 서버로 전달됨

### 경고로 판정 가능

* 외부 font resource 실패
* 테스트 시나리오에서 의도적으로 발생시킨 4xx/5xx
* 카메라 또는 LiDAR hardware가 연결되지 않은 개발 환경
* 시스템 제어 대상이 `unreachable`로 표시되지만 UI가 정상 처리함
* 저장 지도가 없어 목록이 비어 있음

---

## 19. 결과 보고 형식

각 흐름은 다음 형식으로 보고한다.

```text
[PASS|FAIL|WARN] Flow ID - 이름
- URL:
- 초기 상태:
- 사용자 동작:
- 실제 요청:
- 실제 응답:
- 최종 DOM 상태:
- console error:
- failed request:
- screenshot:
- 비고:
```

최종 요약에는 다음을 포함한다.

```text
검증한 flow 수
PASS 수
FAIL 수
WARN 수
발견된 회귀
발견된 안전 문제
실행하지 못한 항목과 이유
실제 외부 시스템 또는 hardware 요청 여부
```
