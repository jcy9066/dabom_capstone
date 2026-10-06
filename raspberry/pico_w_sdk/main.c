#include <stdbool.h>
#include <inttypes.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "hardware/clocks.h"
#include "hardware/gpio.h"
#include "hardware/pwm.h"
#include "hardware/uart.h"
#include "pico/stdlib.h"


/* Pi ↔ Pico UART */
#define CONTROL_UART uart0
#define UART_BAUDRATE 115200
#define UART_TX_PIN 0
#define UART_RX_PIN 1

/* MDD10A 모터 제어 */
#define LEFT_PWM_PIN 2
#define LEFT_DIR_PIN 3
#define RIGHT_PWM_PIN 4
#define RIGHT_DIR_PIN 5

/* 4개 모터 엔코더 */
#define LEFT_FRONT_ENCODER_A_PIN 6
#define LEFT_FRONT_ENCODER_B_PIN 8

#define RIGHT_FRONT_ENCODER_A_PIN 10
#define RIGHT_FRONT_ENCODER_B_PIN 11

#define LEFT_REAR_ENCODER_A_PIN 12
#define LEFT_REAR_ENCODER_B_PIN 13

#define RIGHT_REAR_ENCODER_A_PIN 14
#define RIGHT_REAR_ENCODER_B_PIN 15

/* 추가 출력 장치 */
#define SPEAKER_PIN 16
#define MOSFET_PIN 20
#define WARNING_LED_PIN MOSFET_PIN

/*
 * 전진 시 tick이 감소하는 바퀴는 해당 값을 -1로 변경한다.
 */
#define LEFT_FRONT_ENCODER_SIGN 1
#define RIGHT_FRONT_ENCODER_SIGN -1
#define LEFT_REAR_ENCODER_SIGN 1
#define RIGHT_REAR_ENCODER_SIGN -1

#define ENCODER_REPORT_INTERVAL_MS 50U

#define PWM_FREQUENCY_HZ 20000.0f
#define PWM_WRAP 999U

#define COMMAND_TIMEOUT_MS 350U
#define WARNING_LED_FAILSAFE_TIMEOUT_MS 12000U
#define BEEP_FREQUENCY_HZ 440.0f
#define BEEP_PWM_WRAP 4095U
#define BEEP_MIN_DURATION_MS 50U
#define BEEP_MAX_DURATION_MS 2000U
#define CURVE_INNER_RATIO 0.35f

/*
 * MDD10A / motor protection.
 *
 * MOTOR_MAX_PWM is a firmware safety ceiling, not a current limiter.
 * Start conservatively and raise only after current/thermal validation.
 */
#define MOTOR_MAX_PWM 0.30f
#define MOTOR_RAMP_STEP 0.05f
#define MOTOR_RAMP_INTERVAL_MS 20U
#define MOTOR_REVERSE_DEADTIME_MS 120U
#define MOTOR_ZERO_EPSILON 0.0001f

#define RX_BUFFER_SIZE 128U

/*
 * 현재 수동 조향에서 사용하던 설정을 유지한다.
 */
#define LEFT_FORWARD_DIR_LEVEL 0
#define RIGHT_FORWARD_DIR_LEVEL 0


typedef struct {
    uint pwm_pin;
    uint dir_pin;
    uint slice;
    uint channel;
    bool forward_dir_level;

    /*
     * current_speed is the PWM magnitude/direction actually being applied.
     * target_speed is the latest requested signed PWM.
     */
    float current_speed;
    float target_speed;

    bool direction_forward;
    bool reverse_waiting;

    uint64_t reverse_deadline_ms;
    uint64_t last_ramp_ms;
} motor_channel_t;


static motor_channel_t left_motor;
static motor_channel_t right_motor;

static bool is_moving = false;
static uint64_t last_move_ms = 0;

/*
 * GPIO IRQ에서 갱신한다.
 * int32_t 읽기/쓰기는 RP2040에서 원자적으로 처리된다.
 */
static volatile int32_t left_front_encoder_ticks = 0;
static volatile int32_t right_front_encoder_ticks = 0;
static volatile int32_t left_rear_encoder_ticks = 0;
static volatile int32_t right_rear_encoder_ticks = 0;

static volatile uint8_t left_front_encoder_state = 0;
static volatile uint8_t right_front_encoder_state = 0;
static volatile uint8_t left_rear_encoder_state = 0;
static volatile uint8_t right_rear_encoder_state = 0;

static bool encoder_stream_enabled = false;
static uint64_t last_encoder_report_ms = 0;
static bool warning_led_enabled = false;
static uint64_t last_warning_led_command_ms = 0;
static uint speaker_pwm_slice = 0;
static uint speaker_pwm_channel = 0;
static bool speaker_beep_active = false;
static uint64_t speaker_beep_until_ms = 0;


/*
 * 이전 AB 상태와 현재 AB 상태로 quadrature 방향을 계산한다.
 *
 * index = previous_state << 2 | current_state
 */
static const int8_t quadrature_table[16] = {
     0,  1, -1,  0,
    -1,  0,  0,  1,
     1,  0,  0, -1,
     0, -1,  1,  0
};


static float clamp_float(
    float value,
    float min_value,
    float max_value
) {
    if (value < min_value) {
        return min_value;
    }

    if (value > max_value) {
        return max_value;
    }

    return value;
}


static void uart_send_line(const char *message) {
    uart_puts(CONTROL_UART, message);
    uart_putc_raw(CONTROL_UART, '\n');
}


static void uart_reply(const char *message) {
    uart_send_line(message);
}


static void set_warning_led(bool enabled) {
    gpio_put(WARNING_LED_PIN, enabled ? 1 : 0);
    warning_led_enabled = enabled;
    last_warning_led_command_ms = to_ms_since_boot(get_absolute_time());
}


static void speaker_stop_beep(void) {
    pwm_set_chan_level(speaker_pwm_slice, speaker_pwm_channel, 0);
    speaker_beep_active = false;
    speaker_beep_until_ms = 0;
}


static void speaker_init(void) {
    gpio_set_function(SPEAKER_PIN, GPIO_FUNC_PWM);
    speaker_pwm_slice = pwm_gpio_to_slice_num(SPEAKER_PIN);
    speaker_pwm_channel = pwm_gpio_to_channel(SPEAKER_PIN);

    const float divider =
        (float)clock_get_hz(clk_sys) /
        (BEEP_FREQUENCY_HZ * ((float)BEEP_PWM_WRAP + 1.0f));

    pwm_set_clkdiv(speaker_pwm_slice, divider);
    pwm_set_wrap(speaker_pwm_slice, BEEP_PWM_WRAP);
    pwm_set_chan_level(speaker_pwm_slice, speaker_pwm_channel, 0);
    pwm_set_enabled(speaker_pwm_slice, true);
    speaker_stop_beep();
}


static void speaker_start_beep(uint32_t duration_ms) {
    pwm_set_chan_level(
        speaker_pwm_slice,
        speaker_pwm_channel,
        BEEP_PWM_WRAP / 2U
    );
    speaker_beep_active = true;
    speaker_beep_until_ms =
        to_ms_since_boot(get_absolute_time()) + (uint64_t)duration_ms;
}


static void speaker_tick(uint64_t now_ms) {
    if (speaker_beep_active && now_ms >= speaker_beep_until_ms) {
        speaker_stop_beep();
    }
}


static uint8_t read_encoder_state(
    uint a_pin,
    uint b_pin
) {
    const uint8_t a = gpio_get(a_pin) ? 1U : 0U;
    const uint8_t b = gpio_get(b_pin) ? 1U : 0U;

    return (uint8_t)((a << 1U) | b);
}


static void update_encoder(
    volatile int32_t *ticks,
    volatile uint8_t *previous_state,
    uint a_pin,
    uint b_pin,
    int sign
) {
    const uint8_t new_state = read_encoder_state(
        a_pin,
        b_pin
    );

    const uint8_t index = (uint8_t)(
        ((*previous_state) << 2U) |
        new_state
    );

    *ticks += quadrature_table[index] * sign;
    *previous_state = new_state;
}


static void encoder_gpio_callback(
    uint gpio,
    uint32_t events
) {
    (void)events;

    if (
        gpio == LEFT_FRONT_ENCODER_A_PIN ||
        gpio == LEFT_FRONT_ENCODER_B_PIN
    ) {
        update_encoder(
            &left_front_encoder_ticks,
            &left_front_encoder_state,
            LEFT_FRONT_ENCODER_A_PIN,
            LEFT_FRONT_ENCODER_B_PIN,
            LEFT_FRONT_ENCODER_SIGN
        );
    }

    if (
        gpio == RIGHT_FRONT_ENCODER_A_PIN ||
        gpio == RIGHT_FRONT_ENCODER_B_PIN
    ) {
        update_encoder(
            &right_front_encoder_ticks,
            &right_front_encoder_state,
            RIGHT_FRONT_ENCODER_A_PIN,
            RIGHT_FRONT_ENCODER_B_PIN,
            RIGHT_FRONT_ENCODER_SIGN
        );
    }

    if (
        gpio == LEFT_REAR_ENCODER_A_PIN ||
        gpio == LEFT_REAR_ENCODER_B_PIN
    ) {
        update_encoder(
            &left_rear_encoder_ticks,
            &left_rear_encoder_state,
            LEFT_REAR_ENCODER_A_PIN,
            LEFT_REAR_ENCODER_B_PIN,
            LEFT_REAR_ENCODER_SIGN
        );
    }

    if (
        gpio == RIGHT_REAR_ENCODER_A_PIN ||
        gpio == RIGHT_REAR_ENCODER_B_PIN
    ) {
        update_encoder(
            &right_rear_encoder_ticks,
            &right_rear_encoder_state,
            RIGHT_REAR_ENCODER_A_PIN,
            RIGHT_REAR_ENCODER_B_PIN,
            RIGHT_REAR_ENCODER_SIGN
        );
    }
}


static void encoder_init(void) {
    const uint encoder_pins[] = {
        LEFT_FRONT_ENCODER_A_PIN,
        LEFT_FRONT_ENCODER_B_PIN,
        RIGHT_FRONT_ENCODER_A_PIN,
        RIGHT_FRONT_ENCODER_B_PIN,
        LEFT_REAR_ENCODER_A_PIN,
        LEFT_REAR_ENCODER_B_PIN,
        RIGHT_REAR_ENCODER_A_PIN,
        RIGHT_REAR_ENCODER_B_PIN
    };

    const size_t encoder_pin_count =
        sizeof(encoder_pins) /
        sizeof(encoder_pins[0]);

    for (
        size_t index = 0;
        index < encoder_pin_count;
        index++
    ) {
        const uint pin = encoder_pins[index];

        gpio_init(pin);
        gpio_set_dir(pin, GPIO_IN);
        gpio_pull_up(pin);
    }

    left_front_encoder_state = read_encoder_state(
        LEFT_FRONT_ENCODER_A_PIN,
        LEFT_FRONT_ENCODER_B_PIN
    );

    right_front_encoder_state = read_encoder_state(
        RIGHT_FRONT_ENCODER_A_PIN,
        RIGHT_FRONT_ENCODER_B_PIN
    );

    left_rear_encoder_state = read_encoder_state(
        LEFT_REAR_ENCODER_A_PIN,
        LEFT_REAR_ENCODER_B_PIN
    );

    right_rear_encoder_state = read_encoder_state(
        RIGHT_REAR_ENCODER_A_PIN,
        RIGHT_REAR_ENCODER_B_PIN
    );

    const uint32_t edge_events =
        GPIO_IRQ_EDGE_RISE |
        GPIO_IRQ_EDGE_FALL;

    gpio_set_irq_enabled_with_callback(
        encoder_pins[0],
        edge_events,
        true,
        &encoder_gpio_callback
    );

    for (
        size_t index = 1;
        index < encoder_pin_count;
        index++
    ) {
        gpio_set_irq_enabled(
            encoder_pins[index],
            edge_events,
            true
        );
    }
}


static void encoder_reset(void) {
    left_front_encoder_ticks = 0;
    right_front_encoder_ticks = 0;
    left_rear_encoder_ticks = 0;
    right_rear_encoder_ticks = 0;
}


static void encoder_snapshot(
    int32_t *left_front,
    int32_t *right_front,
    int32_t *left_rear,
    int32_t *right_rear
) {
    *left_front = left_front_encoder_ticks;
    *right_front = right_front_encoder_ticks;
    *left_rear = left_rear_encoder_ticks;
    *right_rear = right_rear_encoder_ticks;
}


static void send_encoder_response(
    const char *prefix
) {
    int32_t left_front = 0;
    int32_t right_front = 0;
    int32_t left_rear = 0;
    int32_t right_rear = 0;

    encoder_snapshot(
        &left_front,
        &right_front,
        &left_rear,
        &right_rear
    );

    const uint64_t timestamp_ms = to_ms_since_boot(
        get_absolute_time()
    );

    char message[160];

    snprintf(
        message,
        sizeof(message),
        "%s,%ld,%ld,%ld,%ld,%llu",
        prefix,
        (long)left_front,
        (long)right_front,
        (long)left_rear,
        (long)right_rear,
        (unsigned long long)timestamp_ms
    );

    uart_send_line(message);
}


static void send_encoder_event(void) {
    send_encoder_response("EVENT,ENC");
}


static void motor_channel_init(
    motor_channel_t *motor,
    uint pwm_pin,
    uint dir_pin,
    bool forward_dir_level
) {
    motor->pwm_pin = pwm_pin;
    motor->dir_pin = dir_pin;
    motor->forward_dir_level = forward_dir_level;

    gpio_init(dir_pin);
    gpio_set_dir(dir_pin, GPIO_OUT);
    gpio_put(dir_pin, forward_dir_level);

    gpio_set_function(
        pwm_pin,
        GPIO_FUNC_PWM
    );

    motor->slice = pwm_gpio_to_slice_num(
        pwm_pin
    );

    motor->channel = pwm_gpio_to_channel(
        pwm_pin
    );

    pwm_config config = pwm_get_default_config();

    pwm_config_set_wrap(
        &config,
        PWM_WRAP
    );

    const float clock_divider =
        (float)clock_get_hz(clk_sys) /
        (
            PWM_FREQUENCY_HZ *
            (float)(PWM_WRAP + 1U)
        );

    pwm_config_set_clkdiv(
        &config,
        clock_divider
    );

    pwm_init(
        motor->slice,
        &config,
        true
    );

    pwm_set_chan_level(
        motor->slice,
        motor->channel,
        0
    );

    motor->current_speed = 0.0f;
    motor->target_speed = 0.0f;

    motor->direction_forward = true;
    motor->reverse_waiting = false;

    motor->reverse_deadline_ms = 0;
    motor->last_ramp_ms = 0;
}


static float abs_float(float value) {
    return value >= 0.0f ? value : -value;
}


static int speed_sign(float value) {
    if (value > MOTOR_ZERO_EPSILON) {
        return 1;
    }

    if (value < -MOTOR_ZERO_EPSILON) {
        return -1;
    }

    return 0;
}


static float ramp_toward(
    float current,
    float target
) {
    if (current < target) {
        current += MOTOR_RAMP_STEP;

        if (current > target) {
            current = target;
        }

    } else if (current > target) {
        current -= MOTOR_RAMP_STEP;

        if (current < target) {
            current = target;
        }
    }

    return current;
}


static void motor_write_direction(
    motor_channel_t *motor,
    bool forward
) {
    gpio_put(
        motor->dir_pin,
        forward
            ? motor->forward_dir_level
            : !motor->forward_dir_level
    );

    motor->direction_forward = forward;
}


static void motor_write_pwm(
    motor_channel_t *motor,
    float magnitude
) {
    magnitude = clamp_float(
        magnitude,
        0.0f,
        MOTOR_MAX_PWM
    );

    const uint16_t level = (uint16_t)(
        magnitude * (float)PWM_WRAP
    );

    pwm_set_chan_level(
        motor->slice,
        motor->channel,
        level
    );
}


static void motor_force_stop(
    motor_channel_t *motor
) {
    motor->target_speed = 0.0f;
    motor->current_speed = 0.0f;

    motor->reverse_waiting = false;
    motor->reverse_deadline_ms = 0;

    motor_write_pwm(
        motor,
        0.0f
    );
}


static void motor_request_signed_speed(
    motor_channel_t *motor,
    float signed_speed
) {
    /*
     * Even if the host requests +/-1.0,
     * the Pico firmware enforces its own safety ceiling.
     */
    signed_speed = clamp_float(
        signed_speed,
        -MOTOR_MAX_PWM,
        MOTOR_MAX_PWM
    );

    motor->target_speed = signed_speed;
}


static void motor_tick(
    motor_channel_t *motor,
    uint64_t now_ms
) {
    if (
        now_ms - motor->last_ramp_ms <
        MOTOR_RAMP_INTERVAL_MS
    ) {
        return;
    }

    motor->last_ramp_ms = now_ms;

    const float target = motor->target_speed;
    const float current = motor->current_speed;

    const int target_sign = speed_sign(target);
    const int current_sign = speed_sign(current);

    /*
     * Reverse dead-time:
     * PWM must stay at zero until the deadline.
     */
    if (motor->reverse_waiting) {
        motor_write_pwm(
            motor,
            0.0f
        );

        motor->current_speed = 0.0f;

        if (now_ms < motor->reverse_deadline_ms) {
            return;
        }

        /*
         * The requested direction may have changed
         * while we were waiting. Use the latest target.
         */
        const int latest_target_sign =
            speed_sign(
                motor->target_speed
            );

        if (latest_target_sign != 0) {
            motor_write_direction(
                motor,
                latest_target_sign > 0
            );
        }

        motor->reverse_waiting = false;
        return;
    }

    /*
     * Active forward/reverse transition:
     *
     * Keep the old DIR level and ramp PWM all the way
     * to zero before allowing the direction pin to change.
     */
    if (
        current_sign != 0 &&
        target_sign != 0 &&
        current_sign != target_sign
    ) {
        float magnitude =
            abs_float(current);

        magnitude = ramp_toward(
            magnitude,
            0.0f
        );

        motor_write_pwm(
            motor,
            magnitude
        );

        if (
            magnitude <=
            MOTOR_ZERO_EPSILON
        ) {
            motor->current_speed = 0.0f;

            motor_write_pwm(
                motor,
                0.0f
            );

            motor->reverse_waiting = true;

            motor->reverse_deadline_ms =
                now_ms +
                MOTOR_REVERSE_DEADTIME_MS;

        } else {
            motor->current_speed =
                current_sign > 0
                    ? magnitude
                    : -magnitude;
        }

        return;
    }

    /*
     * Starting from zero in the opposite direction:
     * keep PWM at zero, wait, then change DIR.
     */
    if (
        current_sign == 0 &&
        target_sign != 0
    ) {
        const bool desired_forward =
            target_sign > 0;

        if (
            motor->direction_forward !=
            desired_forward
        ) {
            motor_write_pwm(
                motor,
                0.0f
            );

            motor->reverse_waiting = true;

            motor->reverse_deadline_ms =
                now_ms +
                MOTOR_REVERSE_DEADTIME_MS;

            return;
        }
    }

    /*
     * Normal acceleration/deceleration in the
     * currently selected direction.
     */
    const float current_magnitude =
        abs_float(current);

    const float target_magnitude =
        abs_float(target);

    const float next_magnitude =
        ramp_toward(
            current_magnitude,
            target_magnitude
        );

    if (
        next_magnitude <=
        MOTOR_ZERO_EPSILON
    ) {
        motor->current_speed = 0.0f;

        motor_write_pwm(
            motor,
            0.0f
        );

        return;
    }

    const int output_sign =
        target_sign != 0
            ? target_sign
            : current_sign;

    motor->current_speed =
        output_sign > 0
            ? next_magnitude
            : -next_magnitude;

    motor_write_pwm(
        motor,
        next_magnitude
    );
}


static void stop_motors(void) {
    /*
     * STOP / failsafe is intentionally immediate.
     * Do not ramp an emergency stop.
     */
    motor_force_stop(
        &left_motor
    );

    motor_force_stop(
        &right_motor
    );

    is_moving = false;
}


static bool direction_to_wheel_speeds(
    const char *direction,
    float speed,
    float *left_speed,
    float *right_speed
) {
    const float inner =
        speed * CURVE_INNER_RATIO;

    if (strcmp(direction, "forward") == 0) {
        *left_speed = speed;
        *right_speed = speed;

    } else if (
        strcmp(direction, "backward") == 0
    ) {
        *left_speed = -speed;
        *right_speed = -speed;

    } else if (
        strcmp(direction, "left") == 0
    ) {
        *left_speed = 0.0f;
        *right_speed = speed;

    } else if (
        strcmp(direction, "right") == 0
    ) {
        *left_speed = speed;
        *right_speed = 0.0f;

    } else if (
        strcmp(direction, "forward_left") == 0
    ) {
        *left_speed = inner;
        *right_speed = speed;

    } else if (
        strcmp(direction, "forward_right") == 0
    ) {
        *left_speed = speed;
        *right_speed = inner;

    } else if (
        strcmp(direction, "backward_left") == 0
    ) {
        *left_speed = -speed;
        *right_speed = -inner;

    } else if (
        strcmp(direction, "backward_right") == 0
    ) {
        *left_speed = -inner;
        *right_speed = -speed;

    } else if (
        strcmp(direction, "rotate_left") == 0
    ) {
        *left_speed = -speed;
        *right_speed = speed;

    } else if (
        strcmp(direction, "rotate_right") == 0
    ) {
        *left_speed = speed;
        *right_speed = -speed;

    } else {
        return false;
    }

    return true;
}


static void move_motors(
    const char *direction,
    float speed
) {
    float left_speed = 0.0f;
    float right_speed = 0.0f;

    if (
        !direction_to_wheel_speeds(
            direction,
            speed,
            &left_speed,
            &right_speed
        )
    ) {
        stop_motors();
        uart_reply("ERR,invalid direction");
        return;
    }

    motor_request_signed_speed(
        &left_motor,
        left_speed
    );

    motor_request_signed_speed(
        &right_motor,
        right_speed
    );

    last_move_ms = to_ms_since_boot(
        get_absolute_time()
    );

    is_moving = true;

    uart_puts(CONTROL_UART, "OK,MOVE,");
    uart_puts(CONTROL_UART, direction);
    uart_putc_raw(CONTROL_UART, '\n');
}


static void drive_motors(
    float left_speed,
    float right_speed
) {
    /*
     * Explicit zero command is an immediate stop.
     * This also resets any pending reverse transition.
     */
    if (
        abs_float(left_speed) <= MOTOR_ZERO_EPSILON &&
        abs_float(right_speed) <= MOTOR_ZERO_EPSILON
    ) {
        stop_motors();
        uart_reply("OK,DRIVE");
        return;
    }

    /*
     * DRIVE 입력은 -1.0 ~ 1.0 범위의 정규화된 PWM 명령이다.
     * 범위 검증은 command parser에서 먼저 수행한다.
     */
    motor_request_signed_speed(
        &left_motor,
        left_speed
    );

    motor_request_signed_speed(
        &right_motor,
        right_speed
    );

    if (
        left_speed == 0.0f &&
        right_speed == 0.0f
    ) {
        is_moving = false;
    } else {
        last_move_ms = to_ms_since_boot(
            get_absolute_time()
        );

        is_moving = true;
    }

    uart_reply("OK,DRIVE");
}


static bool parse_enabled_value(
    const char *value,
    bool *enabled
) {
    if (
        strcmp(value, "1") == 0 ||
        strcmp(value, "on") == 0 ||
        strcmp(value, "ON") == 0 ||
        strcmp(value, "true") == 0
    ) {
        *enabled = true;
        return true;
    }

    if (
        strcmp(value, "0") == 0 ||
        strcmp(value, "off") == 0 ||
        strcmp(value, "OFF") == 0 ||
        strcmp(value, "false") == 0
    ) {
        *enabled = false;
        return true;
    }

    return false;
}


static void handle_command(char *line) {
    char *save_pointer = NULL;

    char *command = strtok_r(
        line,
        ",",
        &save_pointer
    );

    if (command == NULL) {
        return;
    }

    if (strcmp(command, "PING") == 0) {
        uart_reply("OK,PONG");
        return;
    }

    if (strcmp(command, "FW_INFO") == 0) {
        uart_reply("OK,FW,MOTOR_SAFE_V1");
        return;
    }

    if (strcmp(command, "STOP") == 0) {
        stop_motors();
        uart_reply("OK,STOP");
        return;
    }

    if (strcmp(command, "ENC_GET") == 0) {
        send_encoder_response("OK,ENC");
        return;
    }

    if (strcmp(command, "ENC_RESET") == 0) {
        encoder_reset();
        uart_reply("OK,ENC_RESET");
        return;
    }

    if (strcmp(command, "ENC_STREAM") == 0) {
        char *enabled_text = strtok_r(
            NULL,
            ",",
            &save_pointer
        );

        bool enabled = false;

        if (
            enabled_text == NULL ||
            !parse_enabled_value(
                enabled_text,
                &enabled
            )
        ) {
            uart_reply(
                "ERR,ENC_STREAM requires 0 or 1"
            );
            return;
        }

        encoder_stream_enabled = enabled;

        last_encoder_report_ms =
            to_ms_since_boot(
                get_absolute_time()
            );

        uart_reply(
            enabled
                ? "OK,ENC_STREAM,1"
                : "OK,ENC_STREAM,0"
        );

        return;
    }

    if (strcmp(command, "BEEP") == 0) {
        char *duration_text = strtok_r(NULL, ",", &save_pointer);
        char *extra_argument = strtok_r(NULL, ",", &save_pointer);

        if (duration_text == NULL || extra_argument != NULL) {
            speaker_stop_beep();
            uart_reply("ERR,BEEP requires duration_ms");
            return;
        }

        char *end_pointer = NULL;
        const unsigned long duration_ms = strtoul(
            duration_text,
            &end_pointer,
            10
        );

        if (
            end_pointer == duration_text ||
            *end_pointer != '\0' ||
            duration_ms < BEEP_MIN_DURATION_MS ||
            duration_ms > BEEP_MAX_DURATION_MS
        ) {
            speaker_stop_beep();
            uart_reply("ERR,BEEP duration out of range");
            return;
        }

        speaker_start_beep((uint32_t)duration_ms);

        char response[32];
        snprintf(response, sizeof(response), "OK,BEEP,%lu", duration_ms);
        uart_reply(response);
        return;
    }

    if (strcmp(command, "LED") == 0) {
        char *enabled_text = strtok_r(
            NULL,
            ",",
            &save_pointer
        );
        char *extra_argument = strtok_r(
            NULL,
            ",",
            &save_pointer
        );
        bool enabled = false;
        if (
            enabled_text == NULL ||
            extra_argument != NULL ||
            !parse_enabled_value(enabled_text, &enabled)
        ) {
            set_warning_led(false);
            uart_reply("ERR,LED requires 0 or 1");
            return;
        }
        set_warning_led(enabled);
        uart_reply(enabled ? "OK,LED,1" : "OK,LED,0");
        return;
    }

    if (strcmp(command, "DRIVE") == 0) {
        char *left_text = strtok_r(
            NULL,
            ",",
            &save_pointer
        );

        char *right_text = strtok_r(
            NULL,
            ",",
            &save_pointer
        );

        char *extra_argument = strtok_r(
            NULL,
            ",",
            &save_pointer
        );

        if (
            left_text == NULL ||
            right_text == NULL ||
            extra_argument != NULL
        ) {
            stop_motors();

            uart_reply(
                "ERR,DRIVE requires left and right"
            );

            return;
        }

        char *left_end = NULL;
        char *right_end = NULL;

        const float left_speed = strtof(
            left_text,
            &left_end
        );

        const float right_speed = strtof(
            right_text,
            &right_end
        );

        if (
            left_end == left_text ||
            *left_end != '\0' ||
            right_end == right_text ||
            *right_end != '\0' ||
            !isfinite(left_speed) ||
            !isfinite(right_speed)
        ) {
            stop_motors();
            uart_reply("ERR,invalid DRIVE speed");
            return;
        }

        if (
            left_speed < -1.0f ||
            left_speed > 1.0f ||
            right_speed < -1.0f ||
            right_speed > 1.0f
        ) {
            stop_motors();

            uart_reply(
                "ERR,DRIVE speed out of range"
            );

            return;
        }

        drive_motors(
            left_speed,
            right_speed
        );

        return;
    }

    if (strcmp(command, "MOVE") == 0) {
        char *direction = strtok_r(
            NULL,
            ",",
            &save_pointer
        );

        char *speed_text = strtok_r(
            NULL,
            ",",
            &save_pointer
        );

        char *extra_argument = strtok_r(
            NULL,
            ",",
            &save_pointer
        );

        if (
            direction == NULL ||
            speed_text == NULL ||
            extra_argument != NULL
        ) {
            stop_motors();

            uart_reply(
                "ERR,MOVE requires direction and speed"
            );

            return;
        }

        char *end_pointer = NULL;

        float speed = strtof(
            speed_text,
            &end_pointer
        );

        if (
            end_pointer == speed_text ||
            *end_pointer != '\0' ||
            !isfinite(speed)
        ) {
            stop_motors();
            uart_reply("ERR,invalid speed");
            return;
        }

        speed = clamp_float(
            speed,
            0.0f,
            1.0f
        );

        if (speed <= 0.0f) {
            stop_motors();
            uart_reply("OK,STOP");
            return;
        }

        move_motors(
            direction,
            speed
        );

        return;
    }

    stop_motors();
    uart_reply("ERR,unknown command");
}


int main(void) {
    uart_init(
        CONTROL_UART,
        UART_BAUDRATE
    );

    gpio_set_function(
        UART_TX_PIN,
        GPIO_FUNC_UART
    );

    gpio_set_function(
        UART_RX_PIN,
        GPIO_FUNC_UART
    );

    uart_set_format(
        CONTROL_UART,
        8,
        1,
        UART_PARITY_NONE
    );

    uart_set_fifo_enabled(
        CONTROL_UART,
        true
    );

    motor_channel_init(
        &left_motor,
        LEFT_PWM_PIN,
        LEFT_DIR_PIN,
        LEFT_FORWARD_DIR_LEVEL
    );

    motor_channel_init(
        &right_motor,
        RIGHT_PWM_PIN,
        RIGHT_DIR_PIN,
        RIGHT_FORWARD_DIR_LEVEL
    );

    encoder_init();
    encoder_reset();
    speaker_init();
    gpio_init(WARNING_LED_PIN);
    gpio_set_dir(WARNING_LED_PIN, GPIO_OUT);
    set_warning_led(false);
    stop_motors();

    sleep_ms(100);

    /*
     * 자동 EVENT 출력은 기본적으로 꺼져 있다.
     * 따라서 기존 Pi MotorController와 충돌하지 않는다.
     */
    encoder_stream_enabled = false;

    uart_reply(
        "READY,PICO_W_MOTOR_ENCODER"
    );

    char receive_buffer[RX_BUFFER_SIZE];
    size_t receive_length = 0;

    while (true) {
        while (
            uart_is_readable(CONTROL_UART)
        ) {
            const char character =
                uart_getc(CONTROL_UART);

            if (
                character == '\n' ||
                character == '\r'
            ) {
                if (receive_length > 0) {
                    receive_buffer[
                        receive_length
                    ] = '\0';

                    handle_command(
                        receive_buffer
                    );

                    receive_length = 0;
                }

            } else if (
                receive_length <
                RX_BUFFER_SIZE - 1U
            ) {
                receive_buffer[
                    receive_length++
                ] = character;

            } else {
                receive_length = 0;
                stop_motors();
                set_warning_led(false);

                uart_reply(
                    "ERR,receive buffer overflow"
                );
            }
        }

        const uint64_t now_ms =
            to_ms_since_boot(
                get_absolute_time()
            );

        /*
         * Non-blocking motor protection state machines.
         * UART, encoder, failsafe, LED and speaker handling
         * continue to run while the PWM ramps.
         */
        motor_tick(
            &left_motor,
            now_ms
        );

        motor_tick(
            &right_motor,
            now_ms
        );

        speaker_tick(now_ms);

        if (is_moving) {
            if (
                now_ms - last_move_ms >
                COMMAND_TIMEOUT_MS
            ) {
                stop_motors();

                uart_reply("EVENT,FAILSAFE_STOP");
            }
        }

        if (
            warning_led_enabled &&
            now_ms - last_warning_led_command_ms >
                WARNING_LED_FAILSAFE_TIMEOUT_MS
        ) {
            set_warning_led(false);
            uart_reply("EVENT,LED_FAILSAFE_OFF");
        }

        if (
            encoder_stream_enabled &&
            now_ms - last_encoder_report_ms >=
                ENCODER_REPORT_INTERVAL_MS
        ) {
            last_encoder_report_ms = now_ms;
            send_encoder_event();
        }

        sleep_ms(1);
    }

    return 0;
}
