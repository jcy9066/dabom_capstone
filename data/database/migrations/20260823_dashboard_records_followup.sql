-- Forward-only dashboard records follow-up migration for MySQL 5.7+.
-- Each column is guarded independently so reruns and partial application are safe.

DROP PROCEDURE IF EXISTS `migrate_dashboard_records_followup`;

DELIMITER //
CREATE PROCEDURE `migrate_dashboard_records_followup`()
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'event_log'
          AND COLUMN_NAME = 'image_deleted_at'
    ) THEN
        ALTER TABLE `event_log`
            ADD COLUMN `image_deleted_at` datetime DEFAULT NULL
            COMMENT 'Gallery image soft-delete timestamp'
            AFTER `image_path`;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'action_log'
          AND COLUMN_NAME = 'image_deleted_at'
    ) THEN
        ALTER TABLE `action_log`
            ADD COLUMN `image_deleted_at` datetime DEFAULT NULL
            COMMENT 'Gallery image soft-delete timestamp'
            AFTER `image_path`;
    END IF;
END//
DELIMITER ;

CALL `migrate_dashboard_records_followup`();
DROP PROCEDURE `migrate_dashboard_records_followup`;
