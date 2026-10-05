-- Scenario 13: dynamic SQL whose target table is chosen at run time.
-- Expected: the statement shape is rebuilt but the table name comes from @TableName (partial);
--           a string read from a table cannot be rebuilt at all (unresolved).
CREATE OR ALTER PROCEDURE ops.usp_TruncateAndCount
    @TableName sysname
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @sql nvarchar(max), @cmd nvarchar(max);

    SET @sql = N'TRUNCATE TABLE ' + QUOTENAME(@TableName) + N';';
    EXEC (@sql);

    SELECT TOP (1) @cmd = c.CommandText
    FROM ops.Commands AS c
    WHERE c.Enabled = 1;

    EXEC sp_executesql @cmd;
END
GO
