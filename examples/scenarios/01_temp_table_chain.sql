-- Scenario 01: a chain of temp tables (#a -> #b -> #c) using SELECT INTO and INSERT ... SELECT.
-- Expected: dbo.DailyRevenue.AmountZar <- #c.AmountZar <- #b.Amount * Ref.FxRates.Rate
--           #b.Amount <- #a.Amount <- Sales.Orders.Amount
CREATE OR ALTER PROCEDURE etl.usp_TempTableChain
    @AsOfDate date
AS
BEGIN
    SET NOCOUNT ON;

    -- 1. Orders for the day
    SELECT o.OrderId, o.CustomerId, o.Amount, o.CurrencyCode
    INTO #a
    FROM Sales.Orders AS o
    WHERE o.OrderDate = @AsOfDate;

    -- 2. Attach the customer's region
    SELECT a.OrderId, a.Amount, a.CurrencyCode, c.Region
    INTO #b
    FROM #a AS a
    JOIN Sales.Customers AS c ON c.CustomerId = a.CustomerId;

    -- 3. Convert to rand
    CREATE TABLE #c (OrderId int NOT NULL PRIMARY KEY, Region varchar(20) NULL, AmountZar decimal(18,2) NULL);

    INSERT INTO #c (OrderId, Region, AmountZar)
    SELECT b.OrderId, b.Region, b.Amount * fx.Rate
    FROM #b AS b
    JOIN Ref.FxRates AS fx
      ON fx.CurrencyCode = b.CurrencyCode
     AND fx.RateDate = @AsOfDate;

    -- 4. Publish
    INSERT INTO dbo.DailyRevenue (OrderId, Region, AmountZar, LoadDate)
    SELECT OrderId, Region, AmountZar, @AsOfDate
    FROM #c;
END
GO
