/*
  Migration: one portal URL per state (no duplicates).

  Run in FeeScheduleApp (or your companion DB) after fee_schedule_app_schema.sql.

  1) Removes duplicate rows per state_code, keeping the smallest link_id.
  2) Adds UNIQUE(state_code) if missing.
*/

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

-- Delete duplicates: keep one row per state_code (lowest link_id)
;WITH d AS (
    SELECT link_id,
           ROW_NUMBER() OVER (PARTITION BY state_code ORDER BY link_id ASC) AS rn
    FROM dbo.state_portal_link
)
DELETE FROM dbo.state_portal_link
WHERE link_id IN (SELECT link_id FROM d WHERE rn > 1);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes i
    INNER JOIN sys.tables t ON i.object_id = t.object_id
    WHERE t.name = N'state_portal_link'
      AND i.name = N'UQ_state_portal_link_state_code'
)
BEGIN
    ALTER TABLE dbo.state_portal_link
    ADD CONSTRAINT UQ_state_portal_link_state_code UNIQUE (state_code);
END
GO

-- Optional: drop old non-unique index if it exists (name from initial schema)
IF EXISTS (
    SELECT 1 FROM sys.indexes i
    INNER JOIN sys.tables t ON i.object_id = t.object_id
    WHERE t.name = N'state_portal_link' AND i.name = N'IX_state_portal_link_state'
)
    DROP INDEX IX_state_portal_link_state ON dbo.state_portal_link;
GO
