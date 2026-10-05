-- Scenario 10: a WHILE loop that processes rows in batches and logs progress.
-- Expected: dbo.FactSales.IsDeleted written in a loop; @Rows <- @@ROWCOUNT; dbo.LoadLog.RowsAffected <- @Rows.
CREATE OR ALTER PROCEDURE etl.usp_PurgeInBatches
    @BatchSize int = 5000
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @Rows int = 1, @Batches int = 0;

    WHILE @Rows > 0
    BEGIN
        UPDATE TOP (@BatchSize) f
           SET f.IsDeleted = 1
          FROM dbo.FactSales AS f
         WHERE f.IsDeleted = 0
           AND NOT EXISTS (SELECT 1 FROM Sales.Orders AS o WHERE o.OrderId = f.OrderId);

        SET @Rows = @@ROWCOUNT;
        SET @Batches += 1;

        INSERT INTO dbo.LoadLog (Step, RowsAffected)
        VALUES ('purge batch ' + CAST(@Batches AS varchar(10)), @Rows);

        IF @Batches >= 100 BREAK;
    END;
END
GO
