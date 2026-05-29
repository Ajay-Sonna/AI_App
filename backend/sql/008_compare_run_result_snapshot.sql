/*
  Store UI replay snapshot for saved compare runs.

  USE FeeScheduleApp;
  GO
*/

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF COL_LENGTH(N'dbo.fee_schedule_compare_run', N'result_snapshot_json') IS NULL
BEGIN
    ALTER TABLE dbo.fee_schedule_compare_run ADD result_snapshot_json NVARCHAR(MAX) NULL;
END
GO
