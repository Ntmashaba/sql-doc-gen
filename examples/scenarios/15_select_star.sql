-- Scenario 15: SELECT * with and without a schema.
-- Expected (procedure only): #cust columns unknown, dbo.CustomerSnapshot written with all columns (Partial).
-- Expected (with schema): * expands to the five Sales.Customers columns and every column resolves.
CREATE OR ALTER PROCEDURE crm.usp_CopyCustomers
AS
BEGIN
    SET NOCOUNT ON;

    SELECT * INTO #cust FROM Sales.Customers WHERE Region IS NOT NULL;

    UPDATE #cust SET CreditLimit = 0 WHERE CreditLimit < 0;

    INSERT INTO dbo.CustomerSnapshot
    SELECT * FROM #cust;

    SELECT TOP (10) CustomerId, CreditLimit FROM #cust;
END
GO
