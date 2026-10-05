-- Scenario 02: INSERT ... EXEC fills a temp table from another procedure's result set.
-- Expected (procedure only): #rates.Rate comes from the result of Ref.usp_GetRates (opaque).
-- Expected (project mode): #rates.Rate <- Ref.FxRates.Rate through the expanded call.
CREATE OR ALTER PROCEDURE Ref.usp_GetRates
    @RateDate date
AS
BEGIN
    SET NOCOUNT ON;
    SELECT fx.CurrencyCode, fx.Rate
    FROM Ref.FxRates AS fx
    WHERE fx.RateDate = @RateDate;
END
GO
CREATE OR ALTER PROCEDURE etl.usp_RepriceProducts
    @RateDate date
AS
BEGIN
    SET NOCOUNT ON;

    CREATE TABLE #rates (CurrencyCode char(3) NOT NULL, Rate decimal(18,6) NOT NULL);

    INSERT INTO #rates (CurrencyCode, Rate)
    EXEC Ref.usp_GetRates @RateDate = @RateDate;

    UPDATE p
       SET p.PriceZar = p.Price * r.Rate
      FROM dbo.Prices AS p
      JOIN #rates AS r ON r.CurrencyCode = p.CurrencyCode;
END
GO
