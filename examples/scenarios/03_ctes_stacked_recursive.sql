-- Scenario 03: stacked CTEs and a recursive CTE.
-- Expected: dbo.ProductSummary.Revenue <- SUM(Sales.OrderLines.Quantity * UnitPrice) via CTE Lines -> ByCategory
--           dbo.OrgChart.Level <- recursive CTE Tree (anchor 0, then Level + 1)
CREATE OR ALTER PROCEDURE rpt.usp_Summaries
AS
BEGIN
    SET NOCOUNT ON;

    WITH Lines AS (
        SELECT ol.ProductId, ol.Quantity * ol.UnitPrice AS LineValue
        FROM Sales.OrderLines AS ol
        JOIN Sales.Orders AS o ON o.OrderId = ol.OrderId
        WHERE o.Status <> 'Cancelled'
    ),
    ByCategory AS (
        SELECT p.Category, COUNT(DISTINCT l.ProductId) AS Products, SUM(l.LineValue) AS Revenue
        FROM Lines AS l
        JOIN Ref.Products AS p ON p.ProductId = l.ProductId
        GROUP BY p.Category
    ),
    Ranked AS (
        SELECT p.Category, p.ProductCode,
               ROW_NUMBER() OVER (PARTITION BY p.Category ORDER BY SUM(l.LineValue) DESC) AS rn
        FROM Lines AS l
        JOIN Ref.Products AS p ON p.ProductId = l.ProductId
        GROUP BY p.Category, p.ProductCode
    )
    INSERT INTO dbo.ProductSummary (Category, Products, Revenue, TopProduct)
    SELECT b.Category, b.Products, b.Revenue, r.ProductCode
    FROM ByCategory AS b
    LEFT JOIN Ranked AS r ON r.Category = b.Category AND r.rn = 1;

    WITH Tree AS (
        SELECT e.EmployeeId, e.ManagerId, 0 AS Level, CAST(e.FullName AS nvarchar(4000)) AS Path
        FROM Ref.Employees AS e
        WHERE e.ManagerId IS NULL
        UNION ALL
        SELECT e.EmployeeId, e.ManagerId, t.Level + 1, CAST(t.Path + N' / ' + e.FullName AS nvarchar(4000))
        FROM Ref.Employees AS e
        JOIN Tree AS t ON t.EmployeeId = e.ManagerId
    )
    INSERT INTO dbo.OrgChart (EmployeeId, ManagerId, Level, Path)
    SELECT EmployeeId, ManagerId, Level, Path FROM Tree
    OPTION (MAXRECURSION 100);
END
GO
