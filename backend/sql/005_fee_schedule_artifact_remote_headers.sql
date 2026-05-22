-- Optional HTTP cache metadata for incremental artifact downloads (skip unchanged files).
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF COL_LENGTH(N'dbo.fee_schedule_artifact', N'remote_etag') IS NULL
BEGIN
    ALTER TABLE dbo.fee_schedule_artifact ADD remote_etag NVARCHAR(256) NULL;
END
GO

IF COL_LENGTH(N'dbo.fee_schedule_artifact', N'remote_last_modified_utc') IS NULL
BEGIN
    ALTER TABLE dbo.fee_schedule_artifact ADD remote_last_modified_utc DATETIME2 NULL;
END
GO
