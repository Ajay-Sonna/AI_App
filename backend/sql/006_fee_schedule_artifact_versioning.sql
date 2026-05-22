/* Fee schedule artifact: date-primary versioning (portal effective date + superseded hint). */

SET ANSI_NULLS ON;
GO
SET QUOTED_IDENTIFIER ON;
GO

IF COL_LENGTH(N'dbo.fee_schedule_artifact', N'portal_effective_date') IS NULL
BEGIN
    ALTER TABLE dbo.fee_schedule_artifact ADD portal_effective_date DATE NULL;
END
GO

IF COL_LENGTH(N'dbo.fee_schedule_artifact', N'effective_date_source') IS NULL
BEGIN
    ALTER TABLE dbo.fee_schedule_artifact ADD effective_date_source NVARCHAR(32) NULL;
END
GO

IF COL_LENGTH(N'dbo.fee_schedule_artifact', N'is_superseded_hint') IS NULL
BEGIN
    ALTER TABLE dbo.fee_schedule_artifact ADD is_superseded_hint BIT NOT NULL CONSTRAINT DF_fee_schedule_artifact_sup DEFAULT (0);
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'IX_fee_schedule_artifact_state_lsk_date'
      AND object_id = OBJECT_ID(N'dbo.fee_schedule_artifact')
)
BEGIN
    CREATE INDEX IX_fee_schedule_artifact_state_lsk_date
        ON dbo.fee_schedule_artifact (state_code, logical_schedule_key, portal_effective_date DESC, fetched_at_utc DESC);
END
GO
