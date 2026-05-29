/*
  Companion database for fee-schedule tooling (NOT the DST warehouse).

  1. Create an empty database on your SQL Server instance, e.g.:
       CREATE DATABASE FeeScheduleApp;
  2. Run this script in that database (SSMS: USE FeeScheduleApp; then execute).

  Connection from the API: same server/login as DST, set env:
    MSSQL_APP_DATABASE=FeeScheduleApp
  Optional full override:
    MSSQL_APP_ODBC_CONN=Driver={...};Server=...;Database=FeeScheduleApp;...
*/

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF OBJECT_ID(N'dbo.fee_schedule_artifact', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fee_schedule_artifact (
        artifact_id BIGINT IDENTITY(1, 1) NOT NULL PRIMARY KEY,
        state_code NVARCHAR(8) NULL,
        logical_schedule_key NVARCHAR(256) NULL,
        source_url NVARCHAR(2048) NOT NULL,
        content_sha256 CHAR(64) NOT NULL,
        stored_rel_path NVARCHAR(1024) NOT NULL,
        original_filename NVARCHAR(512) NULL,
        mime_type NVARCHAR(256) NULL,
        bytes_size BIGINT NOT NULL,
        fetched_at_utc DATETIME2 NOT NULL CONSTRAINT DF_fee_schedule_artifact_fetched DEFAULT (SYSUTCDATETIME()),
        is_current BIT NOT NULL CONSTRAINT DF_fee_schedule_artifact_current DEFAULT (0),
        source_label NVARCHAR(512) NULL,
        remote_etag NVARCHAR(256) NULL,
        remote_last_modified_utc DATETIME2 NULL,
        portal_effective_date DATE NULL,
        effective_date_source NVARCHAR(32) NULL,
        is_superseded_hint BIT NOT NULL CONSTRAINT DF_fee_sched_sup DEFAULT (0)
    );
    CREATE INDEX IX_fee_schedule_artifact_state_current
        ON dbo.fee_schedule_artifact (state_code, is_current);
    CREATE INDEX IX_fee_schedule_artifact_state_lsk_date
        ON dbo.fee_schedule_artifact (state_code, logical_schedule_key, portal_effective_date DESC, fetched_at_utc DESC);
END
GO

IF OBJECT_ID(N'dbo.state_portal_link', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.state_portal_link (
        link_id BIGINT IDENTITY(1, 1) NOT NULL PRIMARY KEY,
        state_code NVARCHAR(8) NOT NULL,
        display_label NVARCHAR(256) NOT NULL,
        portal_url NVARCHAR(2048) NOT NULL,
        sort_order INT NOT NULL CONSTRAINT DF_state_portal_link_sort DEFAULT (0),
        created_at_utc DATETIME2 NOT NULL CONSTRAINT DF_state_portal_link_created DEFAULT (SYSUTCDATETIME()),
        updated_at_utc DATETIME2 NULL,
        last_agent_run_at_utc DATETIME2 NULL,
        CONSTRAINT UQ_state_portal_link_state_code UNIQUE (state_code)
    );
END
GO

IF OBJECT_ID(N'dbo.fee_schedule_column_mapping', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fee_schedule_column_mapping (
        mapping_id BIGINT IDENTITY(1, 1) NOT NULL PRIMARY KEY,
        state_code NVARCHAR(8) NOT NULL,
        state_logical_schedule_key NVARCHAR(256) NOT NULL,
        dst_fsname NVARCHAR(256) NOT NULL,
        column_map_json NVARCHAR(MAX) NOT NULL,
        created_at_utc DATETIME2 NOT NULL CONSTRAINT DF_fee_schedule_mapping_created DEFAULT (SYSUTCDATETIME()),
        updated_at_utc DATETIME2 NULL,
        updated_by NVARCHAR(128) NULL,
        CONSTRAINT UQ_fee_schedule_mapping UNIQUE (state_code, state_logical_schedule_key, dst_fsname)
    );
END
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

IF OBJECT_ID(N'dbo.fee_schedule_compare_run', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.fee_schedule_compare_run (
        compare_run_id BIGINT IDENTITY(1, 1) NOT NULL PRIMARY KEY,
        state_code NVARCHAR(8) NOT NULL,
        artifact_id BIGINT NOT NULL,
        mapping_id BIGINT NULL,
        logical_schedule_key NVARCHAR(256) NULL,
        dst_fsname NVARCHAR(256) NOT NULL,
        trigger_source NVARCHAR(16) NOT NULL,
        status NVARCHAR(16) NOT NULL,
        error_message NVARCHAR(2000) NULL,
        summary_json NVARCHAR(MAX) NULL,
        changes_workbook_rel_path NVARCHAR(1024) NULL,
        changes_workbook_bytes BIGINT NULL,
        compared_at_utc DATETIME2 NOT NULL CONSTRAINT DF_fee_compare_run_at DEFAULT (SYSUTCDATETIME()),
        result_snapshot_json NVARCHAR(MAX) NULL
    );
    CREATE INDEX IX_fee_compare_run_state_at
        ON dbo.fee_schedule_compare_run (state_code, compared_at_utc DESC);
    CREATE INDEX IX_fee_compare_run_artifact
        ON dbo.fee_schedule_compare_run (artifact_id, compared_at_utc DESC);
END
GO
