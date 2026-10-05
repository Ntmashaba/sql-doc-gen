-- Scenario 16: dynamic SQL built by pasting caller-supplied text into the statement.
-- Expected: @SortColumn and @CustomerName are reported as SQL injection risks (high); @Top is a
--           number and cannot carry SQL; the safe version (QUOTENAME + sp_executesql parameter)
--           in crm.usp_SearchCustomersSafe is not reported.
CREATE OR ALTER PROCEDURE crm.usp_SearchCustomers
    @CustomerName nvarchar(100),
    @SortColumn   sysname = N'CustomerName',
    @Top          int = 50
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @sql nvarchar(max);

    SET @sql = N'SELECT TOP (' + CAST(@Top AS nvarchar(10)) + N') c.CustomerId, c.CustomerName, c.Region
FROM Sales.Customers AS c
WHERE c.CustomerName LIKE N''%' + @CustomerName + N'%''
ORDER BY ' + @SortColumn + N';';

    EXEC (@sql);
END
GO

CREATE OR ALTER PROCEDURE crm.usp_SearchCustomersSafe
    @CustomerName nvarchar(100),
    @SortColumn   sysname = N'CustomerName',
    @Top          int = 50
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @sql nvarchar(max);

    SET @sql = N'SELECT TOP (@top) c.CustomerId, c.CustomerName, c.Region
FROM Sales.Customers AS c
WHERE c.CustomerName LIKE N''%'' + @name + N''%''
ORDER BY ' + QUOTENAME(@SortColumn) + N';';

    EXEC sp_executesql @sql, N'@top int, @name nvarchar(100)', @top = @Top, @name = @CustomerName;
END
GO
