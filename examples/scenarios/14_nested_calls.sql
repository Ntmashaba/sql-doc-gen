-- Scenario 14: a procedure that calls others (expanded in project mode).
-- Expected (project mode): dbo.CustomerScore.Score <- crm.usp_ScoreOne's computation; @Band OUTPUT copied back.
CREATE OR ALTER PROCEDURE crm.usp_BandFor
    @Score int,
    @Band varchar(10) OUTPUT
AS
BEGIN
    SET @Band = CASE WHEN @Score >= 60 THEN 'Gold' WHEN @Score >= 40 THEN 'Silver' ELSE 'Bronze' END;
END
GO
CREATE OR ALTER PROCEDURE crm.usp_ScoreOne
    @CustomerId int
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @Score int, @Band varchar(10);

    SELECT @Score = COUNT(*) * 10
    FROM Sales.Orders AS o
    WHERE o.CustomerId = @CustomerId;

    EXEC crm.usp_BandFor @Score = @Score, @Band = @Band OUTPUT;

    UPDATE dbo.CustomerScore
       SET Score = @Score, Band = @Band, UpdatedAt = SYSDATETIME()
     WHERE CustomerId = @CustomerId;
END
GO
CREATE OR ALTER PROCEDURE crm.usp_ScoreAll
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @id int = (SELECT MIN(CustomerId) FROM Sales.Customers);
    WHILE @id IS NOT NULL
    BEGIN
        EXEC crm.usp_ScoreOne @CustomerId = @id;
        SET @id = (SELECT MIN(CustomerId) FROM Sales.Customers WHERE CustomerId > @id);
    END;
END
GO
