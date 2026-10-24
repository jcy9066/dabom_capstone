DROP DATABASE IF EXISTS dabom;
CREATE DATABASE dabom
DEFAULT CHARACTER SET utf8mb4
COLLATE utf8mb4_unicode_ci;

USE dabom;

CREATE TABLE `users` (
    `user_id` int NOT NULL AUTO_INCREMENT COMMENT '식별용',
    `email` varchar(100) NOT NULL COMMENT '로그인 ID',
    `password_hash` varchar(255) NOT NULL COMMENT '해시 암호화 저장용',
    `name` varchar(20) NOT NULL,
    `phone_number` varchar(20) DEFAULT NULL,
    `employee_number` int NOT NULL COMMENT '사번',
    `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `login_at` datetime DEFAULT NULL COMMENT '로그인 시점에 백엔드에서 UPDATE',
    `deleted_at` datetime DEFAULT NULL COMMENT '퇴사자 관리',
    `is_deleted` tinyint(1) NOT NULL DEFAULT 0 COMMENT '소프트 삭제',
    PRIMARY KEY (`user_id`),
    UNIQUE KEY `UK_USERS_EMAIL` (`email`),
    UNIQUE KEY `UK_USERS_EMPLOYEE_NUMBER` (`employee_number`),
    KEY `IDX_USERS_EMAIL` (`email`)
) ENGINE=InnoDB
DEFAULT CHARSET=utf8mb4
COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `event_log` (
    `event_id` int NOT NULL AUTO_INCREMENT,
    `event_source` enum('VISION_AI','SYSTEM_MONITOR') NOT NULL COMMENT '1. 대분류: 이벤트 발생 출처 (AI 판단인지, 시스템 모니터링인지 구분)',
    `event_type` enum('INTRUSION','ASSAULT','SYSTEM_ERROR','NETWORK_LOSS','SENSOR_ANOMALY') NOT NULL COMMENT '2. 소분류: 구체적인 이벤트 및 장애 내용 통합',
    `image_path` varchar(255) DEFAULT NULL COMMENT '이벤트 발생 당시 개인정보 보호 처리 이미지의 상대 경로',
    `image_deleted_at` datetime DEFAULT NULL COMMENT 'Gallery image soft-delete timestamp',
    `confidence` float DEFAULT NULL COMMENT 'AI 모델의 확신도 0.0~1.0 (시스템 에러 시 NULL)',
    `gps_lat` double DEFAULT NULL COMMENT '이벤트 발생 시점 위도 스냅샷',
    `gps_lng` double DEFAULT NULL COMMENT '이벤트 발생 시점 경도 스냅샷',
    `gps_alt` double DEFAULT NULL COMMENT '이벤트 발생 시점 고도 스냅샷',
    `lidar_x` float DEFAULT NULL COMMENT '이벤트 발생 시점 로컬 좌표 x',
    `lidar_y` float DEFAULT NULL COMMENT '이벤트 발생 시점 로컬 좌표 y',
    `is_resolved` tinyint(1) NOT NULL DEFAULT 0 COMMENT '0: 미조치, 1: 조치완료',
    `is_reported` tinyint(1) NOT NULL DEFAULT 0 COMMENT '0: 미신고, 1: 경찰 등 외부 신고 완료',
    `reported_at` datetime DEFAULT NULL COMMENT '관리자가 신고 버튼을 누른 정확한 시각',
    `is_alerted` tinyint(1) NOT NULL DEFAULT 0 COMMENT '0: 미경고, 1: 스피커로 자동/수동 경고음(TTS) 송출됨',
    `is_mic_used` tinyint(1) NOT NULL DEFAULT 0 COMMENT '0: 미사용, 1: 관리자가 마이크를 켜서 직접 육성 통신함',
    `is_false_alarm` tinyint(1) NOT NULL DEFAULT 0 COMMENT '0: 실제 상황, 1: AI 오탐지 (관리자 확인 후 변경)',
    `detected_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '이벤트 탐지 시각',
    `deleted_at` datetime DEFAULT NULL COMMENT '이벤트 삭제 시각',
    `is_deleted` tinyint(1) NOT NULL DEFAULT 0 COMMENT '소프트 삭제',
    PRIMARY KEY (`event_id`),
    KEY `IDX_EVENT_LOG_DETECTED_AT` (`detected_at`)
) ENGINE=InnoDB
DEFAULT CHARSET=utf8mb4
COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `system_status` (
    `status_id` int NOT NULL AUTO_INCREMENT,
    `cpu_usage` float DEFAULT NULL COMMENT '% 단위',
    `cpu_temperature` float DEFAULT NULL COMMENT 'Celsius',
    `ram_usage` float DEFAULT NULL COMMENT '% 단위',
    `ping` smallint DEFAULT NULL COMMENT 'ms 단위 (네트워크 지연)',
    `is_autonomous` tinyint(1) NOT NULL DEFAULT 1 COMMENT '1: 자율주행, 0: 수동제어',
    `speed` tinyint DEFAULT 0 COMMENT 'RC카 속도',
    `gps_lat` double DEFAULT NULL COMMENT '상태 수집 시점 위도',
    `gps_lng` double DEFAULT NULL COMMENT '상태 수집 시점 경도',
    `gps_alt` double DEFAULT NULL COMMENT '상태 수집 시점 고도',
    `lidar_x` float DEFAULT NULL COMMENT '상태 수집 시점 로컬 좌표 x',
    `lidar_y` float DEFAULT NULL COMMENT '상태 수집 시점 로컬 좌표 y',
    `recorded_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`status_id`)
) ENGINE=InnoDB
DEFAULT CHARSET=utf8mb4
COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `action_log` (
    `action_id` int NOT NULL AUTO_INCREMENT,
    `user_id` int NOT NULL COMMENT '조치한 관리자 ID',
    `event_id` int DEFAULT NULL COMMENT '관련 이벤트 ID, 없으면 NULL',
    `action_type` enum('WARNING','MANUAL_MOVING','REPORT','COMMUNICATION','NOTE') NOT NULL DEFAULT 'NOTE' COMMENT 'WARNING 시 tts 및 LED 쏘기',
    `description_content` text DEFAULT NULL COMMENT '상세 내용',
    `image_path` varchar(255) DEFAULT NULL COMMENT '관리자 기록 당시 개인정보 보호 처리 이미지의 상대 경로',
    `image_deleted_at` datetime DEFAULT NULL COMMENT 'Gallery image soft-delete timestamp',
    `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `deleted_at` datetime DEFAULT NULL COMMENT '행동 대응 삭제 시각',
    `is_deleted` tinyint(1) NOT NULL DEFAULT 0 COMMENT '소프트 삭제',
    PRIMARY KEY (`action_id`),
    KEY `IDX_ACTION_LOG_USER_ID` (`user_id`),
    KEY `IDX_ACTION_LOG_EVENT_ID` (`event_id`),
    CONSTRAINT `FK_ACTION_LOG_USER`
        FOREIGN KEY (`user_id`) REFERENCES `users` (`user_id`),
    CONSTRAINT `FK_ACTION_LOG_EVENT`
        FOREIGN KEY (`event_id`) REFERENCES `event_log` (`event_id`)
) ENGINE=InnoDB
DEFAULT CHARSET=utf8mb4
COLLATE=utf8mb4_unicode_ci;
