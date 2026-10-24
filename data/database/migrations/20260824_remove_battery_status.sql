-- Forward-only removal of the unavailable RC car battery status field.
-- The guard makes this safe to rerun after a partial or completed deployment.

DROP PROCEDURE IF EXISTS `remove_battery_status`;

DELIMITER //
CREATE PROCEDURE `remove_battery_status`()
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'system_status'
          AND COLUMN_NAME = 'battery_level'
    ) THEN
        ALTER TABLE `system_status` DROP COLUMN `battery_level`;
    END IF;
END//
DELIMITER ;

CALL `remove_battery_status`();
DROP PROCEDURE `remove_battery_status`;
