-- Scenario 05: UPDATE ... FROM with joins, including a join that can match several rows.
-- Expected: dbo.Prices.PriceZar <- dbo.Prices.Price * Ref.FxRates.Rate (rows matched by CurrencyCode only:
--           FxRates has a row per date, so the join can match many rates per price: possible duplicates).
CREATE OR ALTER PROCEDURE etl.usp_UpdatePrices
AS
BEGIN
    SET NOCOUNT ON;

    UPDATE p
       SET p.PriceZar = p.Price * fx.Rate
      FROM dbo.Prices AS p
      INNER JOIN Ref.FxRates AS fx ON fx.CurrencyCode = p.CurrencyCode
     WHERE p.Price > 0;

    UPDATE p
       SET p.PriceZar = rp.ListPrice
      FROM dbo.Prices AS p
      JOIN Ref.Products AS rp ON rp.ProductId = p.ProductId
     WHERE p.CurrencyCode = 'ZAR';
END
GO
