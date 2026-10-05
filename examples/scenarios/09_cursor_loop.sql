-- Scenario 09: a cursor loop that writes one row per employee.
-- Expected: dbo.Commission.Commission <- @Total * 0.05; @Total <- SUM(Sales.Orders.Amount) per @EmployeeId,
--           @EmployeeId <- cursor column EmployeeId <- Ref.Employees.EmployeeId.
CREATE OR ALTER PROCEDURE hr.usp_Commission
    @PeriodEnd date
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @EmployeeId int, @Total decimal(18,2);

    DECLARE emp CURSOR LOCAL FAST_FORWARD FOR
        SELECT e.EmployeeId FROM Ref.Employees AS e WHERE e.ManagerId IS NOT NULL;

    OPEN emp;
    FETCH NEXT FROM emp INTO @EmployeeId;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        SELECT @Total = SUM(o.Amount)
        FROM Sales.Orders AS o
        JOIN Sales.Customers AS c ON c.CustomerId = o.CustomerId
        WHERE o.OrderDate <= @PeriodEnd
          AND c.Segment = 'Managed'
          AND o.CustomerId % 10 = @EmployeeId % 10;

        INSERT INTO dbo.Commission (EmployeeId, PeriodEnd, Commission)
        VALUES (@EmployeeId, @PeriodEnd, ISNULL(@Total, 0) * 0.05);

        FETCH NEXT FROM emp INTO @EmployeeId;
    END;

    CLOSE emp;
    DEALLOCATE emp;
END
GO
