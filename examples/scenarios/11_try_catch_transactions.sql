-- Scenario 11: TRY/CATCH around a transaction, with an error log in CATCH and an early RETURN.
-- Expected: dbo.CustomerSnapshot written inside the transaction; dbo.ErrorLog only in CATCH;
--           return value 0 or -1. A second procedure begins a transaction without TRY/CATCH and can exit
--           with it still open (review issues).
CREATE OR ALTER PROCEDURE crm.usp_SnapshotCustomers
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    BEGIN TRY
        BEGIN TRANSACTION;

        DELETE FROM dbo.CustomerSnapshot;

        INSERT INTO dbo.CustomerSnapshot (CustomerId, CustomerName, Region, Segment, CreditLimit)
        SELECT CustomerId, CustomerName, Region, Segment, CreditLimit
        FROM Sales.Customers WITH (NOLOCK);

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;

        INSERT INTO dbo.ErrorLog (ErrorNumber, ErrorMessage)
        VALUES (ERROR_NUMBER(), ERROR_MESSAGE());

        RETURN -1;
    END CATCH;

    RETURN 0;
END
GO
CREATE OR ALTER PROCEDURE crm.usp_UnsafeTransaction
    @Region varchar(20)
AS
BEGIN
    BEGIN TRAN;

    UPDATE Sales.Customers SET CreditLimit = CreditLimit * 1.1 WHERE Region = @Region;

    IF @@ERROR <> 0
        RETURN 1;          -- leaves the transaction open

    COMMIT;
    RETURN 0;
END
GO
