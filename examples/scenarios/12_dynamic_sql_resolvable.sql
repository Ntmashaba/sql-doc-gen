-- Scenario 12: dynamic SQL that can be rebuilt from literals (and a parameter used as a value).
-- Expected: the dynamic batch is parsed and analysed under step 4: dbo.Prices.PriceZar <- dbo.Prices.Price * @Factor.
CREATE OR ALTER PROCEDURE etl.usp_DynamicReprice
    @Factor decimal(9,4),
    @Currency char(3)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @sql nvarchar(max);

    SET @sql = N'UPDATE dbo.Prices SET PriceZar = Price * @f WHERE CurrencyCode = @c;';
    SET @sql += N' INSERT INTO dbo.LoadLog (Step, RowsAffected) VALUES (''reprice'', @@ROWCOUNT);';

    EXEC sys.sp_executesql @sql, N'@f decimal(9,4), @c char(3)', @f = @Factor, @c = @Currency;
END
GO
