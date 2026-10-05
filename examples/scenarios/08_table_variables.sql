-- Scenario 08: table variables, a scalar variable read from a table, and a result set.
-- Expected: result set column Total <- @lines.LineValue <- Sales.OrderLines.Quantity * UnitPrice
--           @MaxOrder <- MAX(Sales.Orders.OrderId)
CREATE OR ALTER PROCEDURE rpt.usp_OrderTotals
    @CustomerId int
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @MaxOrder int;
    DECLARE @lines TABLE (OrderId int NOT NULL, LineValue decimal(18,2) NOT NULL);

    SELECT @MaxOrder = MAX(o.OrderId)
    FROM Sales.Orders AS o
    WHERE o.CustomerId = @CustomerId;

    INSERT INTO @lines (OrderId, LineValue)
    SELECT ol.OrderId, ol.Quantity * ol.UnitPrice
    FROM Sales.OrderLines AS ol
    WHERE ol.OrderId <= @MaxOrder;

    SELECT l.OrderId, SUM(l.LineValue) AS Total
    FROM @lines AS l
    GROUP BY l.OrderId
    ORDER BY l.OrderId;
END
GO
