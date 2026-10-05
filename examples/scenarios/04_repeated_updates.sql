-- Scenario 04: a column inserted, then updated several times; one update is overwritten unread.
-- Expected: #score.Score versions after steps 3, 4, 5 and 6; step 5's value is overwritten by step 6
--           before anything reads it (review issue). dbo.CustomerScore.Score <- #score.Score (steps 3/4/6 possible).
CREATE OR ALTER PROCEDURE crm.usp_ScoreCustomers
AS
BEGIN
    SET NOCOUNT ON;

    CREATE TABLE #score (CustomerId int NOT NULL PRIMARY KEY, Score int NULL, Band varchar(10) NULL);

    INSERT INTO #score (CustomerId, Score)
    SELECT c.CustomerId, 50
    FROM Sales.Customers AS c;

    UPDATE #score
       SET Score = Score + 20
     WHERE CustomerId IN (SELECT o.CustomerId FROM Sales.Orders AS o WHERE o.Amount > 10000);

    UPDATE #score SET Score = Score - 10;

    UPDATE #score SET Score = 0;

    UPDATE s
       SET s.Band = CASE WHEN s.Score >= 60 THEN 'Gold' WHEN s.Score >= 40 THEN 'Silver' ELSE 'Bronze' END
      FROM #score AS s;

    INSERT INTO dbo.CustomerScore (CustomerId, Score, Band, UpdatedAt)
    SELECT CustomerId, Score, Band, SYSDATETIME() FROM #score;
END
GO
