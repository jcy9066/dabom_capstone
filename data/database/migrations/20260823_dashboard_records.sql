-- Forward-only dashboard records migration for MySQL 5.7+.
-- Guards make reruns safe after a partially applied migration. If both the old
-- and new event image columns exist, stop instead of discarding either value.

DROP PROCEDURE IF EXISTS `migrate_dashboard_records`;

DELIMITER //
CREATE PROCEDURE `migrate_dashboard_records`()
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'event_log'
          AND COLUMN_NAME = 'video_path'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'event_log'
          AND COLUMN_NAME = 'image_path'
    ) THEN
        ALTER TABLE `event_log`
            CHANGE COLUMN `video_path` `image_path` varchar(255) DEFAULT NULL
            COMMENT '이벤트 발생 당시 개인정보 보호 처리 이미지의 상대 경로';
    ELSEIF EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'event_log'
          AND COLUMN_NAME = 'video_path'
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'event_log has both video_path and image_path';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'event_log'
          AND COLUMN_NAME = 'lidar_z'
    ) THEN
        ALTER TABLE `event_log` DROP COLUMN `lidar_z`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'action_log'
          AND COLUMN_NAME = 'image_path'
    ) THEN
        ALTER TABLE `action_log`
            ADD COLUMN `image_path` varchar(255) DEFAULT NULL
            COMMENT '관리자 기록 당시 개인정보 보호 처리 이미지의 상대 경로'
            AFTER `description_content`;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'system_status'
          AND COLUMN_NAME = 'lidar_z'
    ) THEN
        ALTER TABLE `system_status` DROP COLUMN `lidar_z`;
    END IF;
END//
DELIMITER ;

CALL `migrate_dashboard_records`();
DROP PROCEDURE `migrate_dashboard_records`;
