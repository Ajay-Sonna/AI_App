/*
  Persisted compare runs + changed workbook paths (FeeScheduleApp).

  USE FeeScheduleApp;
  GO
*/

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
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
        compared_at_utc DATETIME2 NOT NULL CONSTRAINT DF_fee_compare_run_at DEFAULT (SYSUTCDATETIME())
    );
    CREATE INDEX IX_fee_compare_run_state_at
        ON dbo.fee_schedule_compare_run (state_code, compared_at_utc DESC);
    CREATE INDEX IX_fee_compare_run_artifact
        ON dbo.fee_schedule_compare_run (artifact_id, compared_at_utc DESC);
END
GO
