-- Record last agent run time per state (Fee Schedules / POST /run with state_code).
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF COL_LENGTH(N'dbo.state_portal_link', N'last_agent_run_at_utc') IS NULL
BEGIN
    ALTER TABLE dbo.state_portal_link
    ADD last_agent_run_at_utc DATETIME2 NULL;
END
GO
