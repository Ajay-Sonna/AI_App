/*
  Fee schedule notification contacts (per state). Run in FeeScheduleApp after prior migrations.

  USE FeeScheduleApp;
  GO
*/

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF OBJECT_ID(N'dbo.fee_schedule_notification_contact', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fee_schedule_notification_contact (
        notification_contact_id BIGINT IDENTITY(1, 1) NOT NULL PRIMARY KEY,
        state_code NVARCHAR(8) NOT NULL,
        contact_name NVARCHAR(256) NOT NULL,
        email NVARCHAR(320) NOT NULL,
        team_name NVARCHAR(256) NULL,
        department_name NVARCHAR(256) NULL,
        notifications_enabled BIT NOT NULL CONSTRAINT DF_fee_notif_enabled DEFAULT (1),
        notify_new_state_file BIT NOT NULL CONSTRAINT DF_fee_notif_new_file DEFAULT (1),
        notify_compare_result BIT NOT NULL CONSTRAINT DF_fee_notif_compare DEFAULT (1),
        created_at_utc DATETIME2 NOT NULL CONSTRAINT DF_fee_notif_created DEFAULT (SYSUTCDATETIME()),
        updated_at_utc DATETIME2 NULL,
        CONSTRAINT UQ_fee_notif_state_email UNIQUE (state_code, email)
    );
    CREATE INDEX IX_fee_notif_contact_state
        ON dbo.fee_schedule_notification_contact (state_code);
END
GO
