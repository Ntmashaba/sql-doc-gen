-- Scenario 07: IF/ELSE branches write the same column differently.
-- Expected: dbo.DailyRevenue.AmountZar has two possible sources (Amount * Rate, or Amount when ZAR),
--           each depending on the branch condition @Mode = 'FX'.
CREATE OR ALTER PROCEDURE etl.usp_BranchingRevenue
    @Mode varchar(10) = 'FX',
    @AsOfDate date
AS
BEGIN
    SET NOCOUNT ON;

    CREATE TABLE #rev (OrderId int NOT NULL, Region varchar(20) NULL, AmountZar decimal(18,2) NULL);

    IF @Mode = 'FX'
    BEGIN
        INSERT INTO #rev (OrderId, Region, AmountZar)
        SELECT o.OrderId, c.Region, o.Amount * fx.Rate
        FROM Sales.Orders AS o
        JOIN Sales.Customers AS c ON c.CustomerId = o.CustomerId
        LEFT JOIN Ref.FxRates AS fx ON fx.CurrencyCode = o.CurrencyCode AND fx.RateDate = o.OrderDate
        WHERE o.OrderDate = @AsOfDate
          AND fx.Rate > 0;          -- turns the LEFT JOIN into an inner join
    END
    ELSE
    BEGIN
        INSERT INTO #rev (OrderId, Region, AmountZar)
        SELECT o.OrderId, c.Region, o.Amount
        FROM Sales.Orders AS o
        JOIN Sales.Customers AS c ON c.CustomerId = o.CustomerId
        WHERE o.OrderDate = @AsOfDate
          AND o.CurrencyCode = 'ZAR';
    END;

    INSERT INTO dbo.DailyRevenue (OrderId, Region, AmountZar, LoadDate)
    SELECT OrderId, Region, AmountZar, @AsOfDate FROM #rev;
END
GO
