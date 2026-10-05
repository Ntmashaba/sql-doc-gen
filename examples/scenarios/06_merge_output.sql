-- Scenario 06: MERGE with an OUTPUT clause into an audit table.
-- Expected: dbo.FactSales.NetAmount <- #src.NetAmount <- Sales.Orders.Amount - ISNULL(Discount, 0)
--           dbo.FactSalesAudit.NewNet <- inserted.NetAmount (this MERGE), OldNet <- deleted.NetAmount (before)
CREATE OR ALTER PROCEDURE etl.usp_MergeFactSales
    @FromDate date
AS
BEGIN
    SET NOCOUNT ON;

    SELECT o.OrderId, o.CustomerId,
           o.Amount - ISNULL(o.Discount, 0) AS NetAmount,
           HASHBYTES('SHA2_256', CONCAT(o.Amount, '|', o.Discount)) AS RowHash
    INTO #src
    FROM Sales.Orders AS o
    WHERE o.OrderDate >= @FromDate;

    MERGE dbo.FactSales AS tgt
    USING #src AS src
       ON tgt.OrderId = src.OrderId
    WHEN MATCHED AND tgt.RowHash <> src.RowHash THEN
        UPDATE SET tgt.NetAmount = src.NetAmount, tgt.RowHash = src.RowHash, tgt.LoadedAt = SYSDATETIME()
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (OrderId, CustomerId, NetAmount, RowHash, LoadedAt)
        VALUES (src.OrderId, src.CustomerId, src.NetAmount, src.RowHash, SYSDATETIME())
    WHEN NOT MATCHED BY SOURCE THEN
        UPDATE SET tgt.IsDeleted = 1
    OUTPUT $action, inserted.OrderId, deleted.NetAmount, inserted.NetAmount
      INTO dbo.FactSalesAudit (Action, OrderId, OldNet, NewNet);
END
GO
