/*
================================================================================================
 etl.usp_LoadFactRevenue
 Loads dbo.FactRevenue (one row per order line) from sales orders, in rand (ZAR).

 Nightly: SQL Agent job "DW - Revenue - Nightly" runs it with @Mode = 'INCREMENTAL'.
 Month end: finance runs it with @Mode = 'RESTATE' for the current fiscal period.

 Change history
   2019-03-11  initial version (orders, FX conversion, merge)
   2019-08-02  returns
   2020-01-20  channel discounts moved here from the cube
   2020-06-15  tax rules per region (export orders are zero-rated)
   2021-02-03  cost and margin; handling uplift per category
   2021-09-30  data-quality checks written to etl.DataQualityIssue
   2022-04-12  commission rules per sales team
   2023-01-17  manual adjustments (finance corrections) applied before the merge
   2023-07-05  marketplace channel mapping via configurable channel list
   2024-02-28  region summary for the targets dashboard
   2024-11-19  restatement mode
   2024-12-02  brand royalties, payment fees and shipping cost in the margin; reconciliation
   2025-05-06  adjustments: price corrections converted to rand  (ticket DW-2291)

 This file is synthetic test material for sql-doc-gen. It contains one planted bug.
================================================================================================
*/
CREATE OR ALTER PROCEDURE etl.usp_LoadFactRevenue
    @LoadDate           date,
    @Mode               varchar(20) = 'INCREMENTAL',    -- INCREMENTAL | FULL | RESTATE
    @DaysBack           int         = 3,
    @IncludeReturns     bit         = 1,
    @IncludeAdjustments bit         = 1,
    @Debug              bit         = 0,
    @BatchId            int         = NULL OUTPUT,
    @RowsLoaded         int         = NULL OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    /* ==========================================================================================
       0. Variables
       ========================================================================================== */
    DECLARE @ProcName       sysname        = N'etl.usp_LoadFactRevenue';
    DECLARE @StartedAt      datetime2(3)   = SYSDATETIME();
    DECLARE @FromDate       date;
    DECLARE @ToDate         date;
    DECLARE @FiscalYear     int;
    DECLARE @FiscalPeriod   int;
    DECLARE @Rows           int            = 0;
    DECLARE @DqIssues       int            = 0;
    DECLARE @Msg            nvarchar(4000);
    DECLARE @sql            nvarchar(max);
    DECLARE @MarketplaceChannels nvarchar(400);
    DECLARE @DefaultFxRate  decimal(18,8)  = 1.0;
    DECLARE @MaxDiscountPct decimal(9,4)   = 0.5000;
    DECLARE @Region         char(2);
    DECLARE @RegionNet      decimal(18,2);
    DECLARE @RegionTarget   decimal(18,2);
    DECLARE @PeriodMonth    date;

    IF @LoadDate IS NULL
        THROW 50001, N'@LoadDate is required', 1;

    IF @Mode NOT IN ('INCREMENTAL', 'FULL', 'RESTATE')
        THROW 50002, N'@Mode must be INCREMENTAL, FULL or RESTATE', 1;

    /* ==========================================================================================
       1. Batch and date range
       ========================================================================================== */
    INSERT INTO etl.LoadBatch (ProcName, LoadDate, Mode, StartedAt, Status)
    VALUES (@ProcName, @LoadDate, @Mode, @StartedAt, 'RUNNING');

    SET @BatchId = SCOPE_IDENTITY();

    IF @Mode = 'FULL'
    BEGIN
        SET @FromDate = DATEFROMPARTS(YEAR(@LoadDate), 1, 1);
        SET @ToDate   = @LoadDate;
    END
    ELSE IF @Mode = 'RESTATE'
    BEGIN
        SELECT @FiscalYear   = d.FiscalYear,
               @FiscalPeriod = d.FiscalPeriod
          FROM dbo.DimDate AS d
         WHERE d.CalendarDate = @LoadDate;

        SELECT @FromDate = MIN(d.CalendarDate),
               @ToDate   = MAX(d.CalendarDate)
          FROM dbo.DimDate AS d
         WHERE d.FiscalYear   = @FiscalYear
           AND d.FiscalPeriod = @FiscalPeriod;
    END
    ELSE
    BEGIN
        SET @FromDate = DATEADD(DAY, -@DaysBack, @LoadDate);
        SET @ToDate   = @LoadDate;
    END;

    SET @PeriodMonth = DATEFROMPARTS(YEAR(@ToDate), MONTH(@ToDate), 1);

    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status, Message)
    VALUES (@ProcName, @BatchId, N'1 date range', 0, SYSDATETIME(), 'OK',
            CONCAT(N'From ', @FromDate, N' to ', @ToDate, N' (', @Mode, N')'));

    /* ==========================================================================================
       2. Reference data
       ========================================================================================== */

    -- 2.1 Calendar for the load window
    SELECT d.DateKey,
           d.CalendarDate,
           d.FiscalYear,
           d.FiscalPeriod,
           d.IsWeekend,
           d.IsHoliday
      INTO #Calendar
      FROM dbo.DimDate AS d
     WHERE d.CalendarDate BETWEEN @FromDate AND @ToDate;

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'2.1 calendar', @Rows, SYSDATETIME(), 'OK');

    -- 2.2 Daily exchange rates: every currency on every day, carrying the last known rate forward
    SELECT cal.CalendarDate AS RateDate,
           cur.CurrencyCode,
           r.RateToZAR
      INTO #FxDaily
      FROM #Calendar AS cal
     CROSS JOIN (SELECT DISTINCT fx.CurrencyCode FROM ref.FxRates AS fx) AS cur
     OUTER APPLY (SELECT TOP (1) fx.RateToZAR
                    FROM ref.FxRates AS fx
                   WHERE fx.CurrencyCode = cur.CurrencyCode
                     AND fx.RateDate    <= cal.CalendarDate
                   ORDER BY fx.RateDate DESC) AS r;

    -- rand needs no conversion
    UPDATE #FxDaily
       SET RateToZAR = @DefaultFxRate
     WHERE CurrencyCode = 'ZAR';

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'2.2 fx daily', @Rows, SYSDATETIME(), 'OK');

    -- 2.3 Active products
    SELECT p.ProductId,
           p.ProductCode,
           p.ProductName,
           p.CategoryCode,
           p.BrandCode
      INTO #Product
      FROM ref.Product AS p
     WHERE p.IsActive = 1;

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'2.3 products', @Rows, SYSDATETIME(), 'OK');

    -- 2.4 Customers
    SELECT c.CustomerId,
           c.CustomerCode,
           c.Segment,
           c.RegionCode,
           c.IsInternal,
           c.CreditStatus
      INTO #Customer
      FROM sales.Customer AS c;

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'2.4 customers', @Rows, SYSDATETIME(), 'OK');

    -- 2.5 Channels and their commission defaults
    SELECT ch.ChannelCode,
           ch.ChannelName,
           ch.ChannelGroup,
           ch.CommissionPct
      INTO #Channel
      FROM ref.Channel AS ch;

    -- 2.6 Regions
    DECLARE @Regions TABLE
    (
        RegionCode  char(2)      NOT NULL PRIMARY KEY,
        RegionName  nvarchar(100) NOT NULL,
        Territory   varchar(20)  NULL,
        IsEU        bit          NOT NULL
    );

    INSERT INTO @Regions (RegionCode, RegionName, Territory, IsEU)
    SELECT r.RegionCode, r.RegionName, r.Territory, r.IsEU
      FROM ref.Region AS r;

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'2.6 regions', @Rows, SYSDATETIME(), 'OK');

    /* ==========================================================================================
       3. Order lines in the window
       ========================================================================================== */
    CREATE TABLE #Orders
    (
        OrderId         int            NOT NULL,
        LineNumber      int            NOT NULL,
        OrderNumber     varchar(20)    NOT NULL,
        OrderDate       date           NOT NULL,
        ShipDate        date           NULL,
        DateKey         int            NULL,
        CustomerId      int            NOT NULL,
        ProductId       int            NOT NULL,
        ChannelCode     varchar(20)    NOT NULL,
        RegionCode      char(2)        NULL,
        CurrencyCode    char(3)        NOT NULL,
        SalesRepId      int            NULL,
        PromoCode       varchar(20)    NULL,
        Segment         varchar(20)    NULL,
        IsInternal      bit            NULL,
        Quantity        int            NOT NULL,
        UnitPrice       decimal(18,4)  NOT NULL,
        TaxRate         decimal(9,4)   NULL,
        GrossAmount     decimal(18,4)  NOT NULL,
        DiscountAmount  decimal(18,4)  NOT NULL,
        NetAmount       decimal(18,4)  NULL,
        TaxAmount       decimal(18,4)  NULL,
        FxRate          decimal(18,8)  NULL,
        GrossAmountZAR  decimal(18,2)  NULL,
        DiscountZAR     decimal(18,2)  NULL,
        NetAmountZAR    decimal(18,2)  NULL,
        TaxZAR          decimal(18,2)  NULL,
        CostZAR         decimal(18,2)  NULL,
        MarginZAR       decimal(18,2)  NULL,
        ReturnedQty     int            NULL,
        ReturnedZAR     decimal(18,2)  NULL,
        CommissionZAR   decimal(18,2)  NULL,
        IsAdjusted      bit            NOT NULL DEFAULT (0),
        PRIMARY KEY (OrderId, LineNumber)
    );

    -- 3.1 Order lines changed (incremental) or in range (full / restate)
    INSERT INTO #Orders
        (OrderId, LineNumber, OrderNumber, OrderDate, ShipDate, DateKey, CustomerId, ProductId,
         ChannelCode, RegionCode, CurrencyCode, SalesRepId, PromoCode,
         Quantity, UnitPrice, TaxRate, GrossAmount, DiscountAmount)
    SELECT h.OrderId,
           l.LineNumber,
           h.OrderNumber,
           h.OrderDate,
           h.ShipDate,
           cal.DateKey,
           h.CustomerId,
           l.ProductId,
           h.ChannelCode,
           h.RegionCode,
           h.CurrencyCode,
           h.SalesRepId,
           h.PromoCode,
           l.Quantity,
           l.UnitPrice,
           l.TaxRate,
           l.Quantity * l.UnitPrice                 AS GrossAmount,
           ISNULL(l.LineDiscount, 0)                AS DiscountAmount
      FROM sales.OrderHeader AS h
      JOIN sales.OrderLine   AS l
        ON l.OrderId = h.OrderId
      JOIN #Calendar AS cal
        ON cal.CalendarDate = h.OrderDate
     WHERE h.OrderStatus NOT IN ('CANCELLED', 'DRAFT', 'QUOTE')
       AND (   @Mode <> 'INCREMENTAL'
            OR h.ModifiedAt >= DATEADD(DAY, -1, CAST(@LoadDate AS datetime2(3))));

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'3.1 order lines', @Rows, SYSDATETIME(), 'OK');

    -- 3.2 Late-arriving copies of the same order number: keep the newest
    DELETE o
      FROM #Orders AS o
      JOIN (SELECT x.OrderId,
                   x.LineNumber,
                   ROW_NUMBER() OVER (PARTITION BY x.OrderNumber, x.LineNumber
                                      ORDER BY x.OrderId DESC) AS rn
              FROM #Orders AS x) AS d
        ON d.OrderId    = o.OrderId
       AND d.LineNumber = o.LineNumber
     WHERE d.rn > 1;

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'3.2 duplicates', @Rows, SYSDATETIME(), 'OK');

    -- 3.3 Customer attributes
    UPDATE o
       SET o.Segment    = c.Segment,
           o.IsInternal = c.IsInternal,
           o.RegionCode = COALESCE(o.RegionCode, c.RegionCode)
      FROM #Orders   AS o
      JOIN #Customer AS c
        ON c.CustomerId = o.CustomerId;

    -- 3.4 Internal orders (staff accounts used for testing) never reach the fact table
    DELETE FROM #Orders
     WHERE IsInternal = 1
       AND Segment <> 'STAFF';

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'3.4 internal orders', @Rows, SYSDATETIME(), 'OK');

    -- 3.5 Marketplace orders arrive with the marketplace's own channel code; map them.
    --     The list of marketplace channel codes is configurable here (DW-1873).
    SET @MarketplaceChannels = N'''TAKEALOT'', ''AMAZON'', ''MAKRO_MP'', ''BOB_SHOP''';

    SET @sql = N'UPDATE #Orders SET ChannelCode = ''MARKETPLACE'' WHERE ChannelCode IN ('
             + @MarketplaceChannels + N');';

    IF @Debug = 1
        PRINT @sql;

    EXEC sys.sp_executesql @sql;

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'3.5 marketplace channels', @Rows, SYSDATETIME(), 'OK');

    /* ==========================================================================================
       4. Pricing: promotions, channel discounts, tax
       ========================================================================================== */

    -- 4.1 Promotions valid on the order date
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * p.DiscountPct, 2)
      FROM #Orders AS o
      JOIN sales.Promotion AS p
        ON p.PromoCode = o.PromoCode
       AND o.OrderDate BETWEEN p.ValidFrom AND p.ValidTo
     WHERE p.ChannelCode IS NULL
        OR p.ChannelCode = o.ChannelCode;

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'4.1 promotions', @Rows, SYSDATETIME(), 'OK');

    -- 4.2 Promotions that do not apply to this channel are dropped
    UPDATE o
       SET o.PromoCode = NULL
      FROM #Orders AS o
      JOIN sales.Promotion AS p
        ON p.PromoCode = o.PromoCode
     WHERE p.ChannelCode IS NOT NULL
       AND p.ChannelCode <> o.ChannelCode;

    -- 4.3 Channel discounts (only for lines without a promotion)
    -- 4.3.1 Web shop loyalty discount (ONLINE / CONSUMER, 2.0%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.0200, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'ONLINE'
       AND o.Segment = 'CONSUMER'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.1 ONLINE/CONSUMER: ', @Rows, N' line(s)');

    -- 4.3.2 Web shop small-business discount (ONLINE / SMB, 1.5%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.0150, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'ONLINE'
       AND o.Segment = 'SMB'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.2 ONLINE/SMB: ', @Rows, N' line(s)');

    -- 4.3.3 Mobile app discount (APP / CONSUMER, 3.0%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.0300, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'APP'
       AND o.Segment = 'CONSUMER'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.3 APP/CONSUMER: ', @Rows, N' line(s)');

    -- 4.3.4 Mobile app small-business discount (APP / SMB, 1.0%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.0100, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'APP'
       AND o.Segment = 'SMB'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.4 APP/SMB: ', @Rows, N' line(s)');

    -- 4.3.5 Staff purchase discount (STORE / STAFF, 10.0%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.1000, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'STORE'
       AND o.Segment = 'STAFF'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.5 STORE/STAFF: ', @Rows, N' line(s)');

    -- 4.3.6 In-store card discount (STORE / CONSUMER, 0.5%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.0050, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'STORE'
       AND o.Segment = 'CONSUMER'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.6 STORE/CONSUMER: ', @Rows, N' line(s)');

    -- 4.3.7 Partner rebate (PARTNER / ENTERPRISE, 5.0%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.0500, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'PARTNER'
       AND o.Segment = 'ENTERPRISE'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.7 PARTNER/ENTERPRISE: ', @Rows, N' line(s)');

    -- 4.3.8 Partner small-business rebate (PARTNER / SMB, 3.5%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.0350, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'PARTNER'
       AND o.Segment = 'SMB'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.8 PARTNER/SMB: ', @Rows, N' line(s)');

    -- 4.3.9 Telesales volume discount (TELESALES / ENTERPRISE, 2.5%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.0250, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'TELESALES'
       AND o.Segment = 'ENTERPRISE'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.9 TELESALES/ENTERPRISE: ', @Rows, N' line(s)');

    -- 4.3.10 Telesales small-business discount (TELESALES / SMB, 1.0%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.0100, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'TELESALES'
       AND o.Segment = 'SMB'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.10 TELESALES/SMB: ', @Rows, N' line(s)');

    -- 4.3.11 Contract discount (B2B / ENTERPRISE, 4.0%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.0400, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'B2B'
       AND o.Segment = 'ENTERPRISE'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.11 B2B/ENTERPRISE: ', @Rows, N' line(s)');

    -- 4.3.12 Tender discount (B2B / GOVERNMENT, 6.0%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.0600, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'B2B'
       AND o.Segment = 'GOVERNMENT'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.12 B2B/GOVERNMENT: ', @Rows, N' line(s)');

    -- 4.3.13 Export incentive (EXPORT / ENTERPRISE, 2.0%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.0200, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'EXPORT'
       AND o.Segment = 'ENTERPRISE'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.13 EXPORT/ENTERPRISE: ', @Rows, N' line(s)');

    -- 4.3.14 Marketplace orders (fees are booked separately) (MARKETPLACE / CONSUMER, 0.0%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * 0.0000, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = 'MARKETPLACE'
       AND o.Segment = 'CONSUMER'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.14 MARKETPLACE/CONSUMER: ', @Rows, N' line(s)');

    -- 4.4 A discount can never be more than half of the gross amount
    UPDATE #Orders
       SET DiscountAmount = CASE
                                WHEN DiscountAmount > GrossAmount * @MaxDiscountPct
                                    THEN ROUND(GrossAmount * @MaxDiscountPct, 2)
                                ELSE DiscountAmount
                            END;

    -- 4.5 Net amount in order currency
    UPDATE #Orders
       SET NetAmount = GrossAmount - DiscountAmount;

    -- 4.6 Tax rate per region where the order line has none
    -- 4.6.1 Gauteng
    UPDATE #Orders
       SET TaxRate = 0.1500
     WHERE RegionCode = 'GP'
       AND TaxRate IS NULL;

    -- 4.6.2 Western Cape
    UPDATE #Orders
       SET TaxRate = 0.1500
     WHERE RegionCode = 'WC'
       AND TaxRate IS NULL;

    -- 4.6.3 KwaZulu-Natal
    UPDATE #Orders
       SET TaxRate = 0.1500
     WHERE RegionCode = 'KN'
       AND TaxRate IS NULL;

    -- 4.6.4 Eastern Cape
    UPDATE #Orders
       SET TaxRate = 0.1500
     WHERE RegionCode = 'EC'
       AND TaxRate IS NULL;

    -- 4.6.5 Free State
    UPDATE #Orders
       SET TaxRate = 0.1500
     WHERE RegionCode = 'FS'
       AND TaxRate IS NULL;

    -- 4.6.6 Limpopo
    UPDATE #Orders
       SET TaxRate = 0.1500
     WHERE RegionCode = 'LP'
       AND TaxRate IS NULL;

    -- 4.6.7 Mpumalanga
    UPDATE #Orders
       SET TaxRate = 0.1500
     WHERE RegionCode = 'MP'
       AND TaxRate IS NULL;

    -- 4.6.8 North West
    UPDATE #Orders
       SET TaxRate = 0.1500
     WHERE RegionCode = 'NW'
       AND TaxRate IS NULL;

    -- 4.6.9 Northern Cape
    UPDATE #Orders
       SET TaxRate = 0.1500
     WHERE RegionCode = 'NC'
       AND TaxRate IS NULL;

    -- 4.6.10 Namibia (export)
    UPDATE #Orders
       SET TaxRate = 0.0000
     WHERE RegionCode = 'NA'
       AND TaxRate IS NULL;

    -- 4.6.11 Botswana (export)
    UPDATE #Orders
       SET TaxRate = 0.0000
     WHERE RegionCode = 'BW'
       AND TaxRate IS NULL;

    -- 4.6.12 Germany (export)
    UPDATE #Orders
       SET TaxRate = 0.0000
     WHERE RegionCode = 'DE'
       AND TaxRate IS NULL;

    -- 4.6.13 Netherlands (export)
    UPDATE #Orders
       SET TaxRate = 0.0000
     WHERE RegionCode = 'NL'
       AND TaxRate IS NULL;

    -- 4.6.14 France (export)
    UPDATE #Orders
       SET TaxRate = 0.0000
     WHERE RegionCode = 'FR'
       AND TaxRate IS NULL;

    -- 4.6.15 United Kingdom (export)
    UPDATE #Orders
       SET TaxRate = 0.0000
     WHERE RegionCode = 'GB'
       AND TaxRate IS NULL;

    -- 4.6.16 United States (export)
    UPDATE #Orders
       SET TaxRate = 0.0000
     WHERE RegionCode = 'US'
       AND TaxRate IS NULL;

    UPDATE #Orders
       SET TaxAmount = ROUND(NetAmount * ISNULL(TaxRate, 0), 2);

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'4 pricing', @Rows, SYSDATETIME(), 'OK');

    IF @Debug = 1
    BEGIN
        SELECT N'after pricing'        AS Stage,
               o.ChannelCode,
               COUNT(*)          AS Lines,
               SUM(o.GrossAmount)    AS GrossAmount,
               SUM(o.DiscountAmount) AS DiscountAmount,
               SUM(o.NetAmount)      AS NetAmount
          FROM #Orders AS o
         GROUP BY o.ChannelCode
         ORDER BY o.ChannelCode;
    END;

    /* ==========================================================================================
       5. Conversion to rand
       ========================================================================================== */
    UPDATE o
       SET o.FxRate         = fx.RateToZAR,
           o.GrossAmountZAR = ROUND(o.GrossAmount    * fx.RateToZAR, 2),
           o.DiscountZAR    = ROUND(o.DiscountAmount * fx.RateToZAR, 2),
           o.NetAmountZAR   = ROUND(o.NetAmount      * fx.RateToZAR, 2),
           o.TaxZAR         = ROUND(o.TaxAmount      * fx.RateToZAR, 2)
      FROM #Orders  AS o
      JOIN #FxDaily AS fx
        ON fx.CurrencyCode = o.CurrencyCode
       AND fx.RateDate     = o.OrderDate;

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'5.1 conversion', @Rows, SYSDATETIME(), 'OK');

    -- 5.2 Lines without a rate are reported and loaded with a rate of 1 (finance corrects them later)
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'FX_DEFAULTED', o.OrderId, o.LineNumber, 'FxRate',
           CONCAT(N'No rate for ', o.CurrencyCode, N' on ', o.OrderDate, N'; loaded at 1.0'),
           'HIGH', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.FxRate IS NULL;

    UPDATE #Orders
       SET FxRate         = @DefaultFxRate,
           GrossAmountZAR = ROUND(GrossAmount, 2),
           DiscountZAR    = ROUND(DiscountAmount, 2),
           NetAmountZAR   = ROUND(NetAmount, 2),
           TaxZAR         = ROUND(TaxAmount, 2)
     WHERE FxRate IS NULL;

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'5.2 default rates', @Rows, SYSDATETIME(), 'OK');

    IF @Debug = 1
    BEGIN
        SELECT N'after conversion'        AS Stage,
               o.ChannelCode,
               COUNT(*)          AS Lines,
               SUM(o.GrossAmountZAR) AS GrossAmountZAR,
               SUM(o.NetAmountZAR)   AS NetAmountZAR,
               SUM(o.TaxZAR)         AS TaxZAR
          FROM #Orders AS o
         GROUP BY o.ChannelCode
         ORDER BY o.ChannelCode;
    END;

    /* ==========================================================================================
       6. Returns
       ========================================================================================== */
    IF @IncludeReturns = 1
    BEGIN
        SELECT r.OrderId,
               r.LineNumber,
               SUM(r.ReturnQty)    AS ReturnQty,
               SUM(r.RefundAmount) AS RefundAmount
          INTO #Returns
          FROM sales.ReturnLine AS r
         WHERE r.ReturnDate BETWEEN @FromDate AND @ToDate
         GROUP BY r.OrderId, r.LineNumber;

        UPDATE o
           SET o.ReturnedQty = r.ReturnQty,
               o.ReturnedZAR = ROUND(r.RefundAmount * o.FxRate, 2)
          FROM #Orders  AS o
          JOIN #Returns AS r
            ON r.OrderId    = o.OrderId
           AND r.LineNumber = o.LineNumber;

        SET @Rows = @@ROWCOUNT;
        INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
        VALUES (@ProcName, @BatchId, N'6 returns', @Rows, SYSDATETIME(), 'OK');

    END;

    UPDATE #Orders
       SET ReturnedQty = ISNULL(ReturnedQty, 0),
           ReturnedZAR = ISNULL(ReturnedZAR, 0);

    /* ==========================================================================================
       7. Manual adjustments approved by finance
       ========================================================================================== */
    IF @IncludeAdjustments = 1
    BEGIN
        SELECT a.AdjustmentId,
               a.OrderId,
               a.LineNumber,
               a.AdjustmentType,
               a.Amount,
               a.CurrencyCode,
               a.ApprovedBy
          INTO #Adjustments
          FROM sales.ManualAdjustment AS a
         WHERE a.IsApproved = 1
           AND a.EffectiveDate BETWEEN @FromDate AND @ToDate;

        SET @Rows = @@ROWCOUNT;
        INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
        VALUES (@ProcName, @BatchId, N'7.1 adjustments', @Rows, SYSDATETIME(), 'OK');

        -- 7.2 Price corrections (DW-2291): add the corrected amount, in rand
        UPDATE o
           SET o.NetAmount    = o.NetAmount + a.Amount,
               o.NetAmountZAR = o.NetAmountZAR * fx.RateToZAR + ROUND(a.Amount * fx.RateToZAR, 2),
               o.IsAdjusted   = 1
          FROM #Orders      AS o
          JOIN #Adjustments AS a
            ON a.OrderId    = o.OrderId
           AND a.LineNumber = o.LineNumber
          JOIN #FxDaily     AS fx
            ON fx.CurrencyCode = o.CurrencyCode
           AND fx.RateDate     = o.OrderDate
         WHERE a.AdjustmentType = 'PRICE';

        SET @Rows = @@ROWCOUNT;
        INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
        VALUES (@ProcName, @BatchId, N'7.2 price corrections', @Rows, SYSDATETIME(), 'OK');

        -- 7.3 Goodwill credits reduce the net amount (already in rand)
        UPDATE o
           SET o.NetAmountZAR = o.NetAmountZAR - a.Amount,
               o.IsAdjusted   = 1
          FROM #Orders      AS o
          JOIN #Adjustments AS a
            ON a.OrderId    = o.OrderId
           AND a.LineNumber = o.LineNumber
         WHERE a.AdjustmentType = 'GOODWILL'
           AND a.CurrencyCode   = 'ZAR';

        SET @Rows = @@ROWCOUNT;
        INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
        VALUES (@ProcName, @BatchId, N'7.3 goodwill credits', @Rows, SYSDATETIME(), 'OK');

        -- 7.4 Tax follows the corrected net amount
        UPDATE #Orders
           SET TaxZAR = ROUND(NetAmountZAR * ISNULL(TaxRate, 0), 2)
         WHERE IsAdjusted = 1;
    END;

    /* ==========================================================================================
       8. Cost and margin
       ========================================================================================== */
    -- 8.1 Unit cost valid on the order date
    UPDATE o
       SET o.CostZAR = ROUND(o.Quantity * c.UnitCostZAR, 2)
      FROM #Orders AS o
     OUTER APPLY (SELECT TOP (1) pc.UnitCostZAR
                    FROM ref.ProductCost AS pc
                   WHERE pc.ProductId     = o.ProductId
                     AND pc.EffectiveFrom <= o.OrderDate
                   ORDER BY pc.EffectiveFrom DESC) AS c;

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'8.1 cost', @Rows, SYSDATETIME(), 'OK');

    -- 8.2 Returned units carry no cost
    UPDATE #Orders
       SET CostZAR = ROUND(CostZAR * (Quantity - ReturnedQty) / NULLIF(Quantity, 0), 2)
     WHERE ReturnedQty > 0;

    -- 8.3 Handling uplift per product category
    -- 8.3.1 Handling uplift: Appliances (4.50%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0450, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'APPLIANCE'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.2 Handling uplift: Televisions (6.00%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0600, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'TV'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.3 Handling uplift: Audio (2.50%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0250, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'AUDIO'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.4 Handling uplift: Phones (1.50%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0150, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'PHONE'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.5 Handling uplift: Laptops (2.00%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0200, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'LAPTOP'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.6 Handling uplift: Tablets (1.50%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0150, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'TABLET'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.7 Handling uplift: Gaming (2.00%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0200, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'GAMING'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.8 Handling uplift: Cameras (2.00%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0200, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'CAMERA'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.9 Handling uplift: Smart home (3.00%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0300, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'SMARTHOME'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.10 Handling uplift: Furniture (8.00%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0800, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'FURNITURE'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.11 Handling uplift: Garden (7.00%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0700, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'GARDEN'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.12 Handling uplift: Tools (3.50%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0350, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'TOOLS'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.13 Handling uplift: Sport (3.00%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0300, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'SPORT'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.14 Handling uplift: Toys (2.50%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0250, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'TOYS'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.15 Handling uplift: Books (1.00%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0100, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'BOOKS'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.16 Handling uplift: Grocery (5.00%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0500, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'GROCERY'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.17 Handling uplift: Health (2.00%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0200, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'HEALTH'
       AND o.CostZAR IS NOT NULL;

    -- 8.3.18 Handling uplift: Accessories (1.00%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * 0.0100, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = 'ACCESSORY'
       AND o.CostZAR IS NOT NULL;

    -- 8.4 Shipping cost per region (lines that were shipped)
    -- 8.7.1 Shipping cost per line: region GP (R45.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 45.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'GP'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.2 Shipping cost per line: region WC (R65.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 65.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'WC'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.3 Shipping cost per line: region KN (R60.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 60.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'KN'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.4 Shipping cost per line: region EC (R75.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 75.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'EC'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.5 Shipping cost per line: region FS (R70.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 70.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'FS'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.6 Shipping cost per line: region LP (R80.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 80.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'LP'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.7 Shipping cost per line: region MP (R70.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 70.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'MP'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.8 Shipping cost per line: region NW (R70.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 70.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'NW'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.9 Shipping cost per line: region NC (R95.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 95.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'NC'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.10 Shipping cost per line: region NA (R350.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 350.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'NA'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.11 Shipping cost per line: region BW (R300.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 300.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'BW'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.12 Shipping cost per line: region DE (R1250.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 1250.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'DE'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.13 Shipping cost per line: region NL (R1250.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 1250.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'NL'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.14 Shipping cost per line: region FR (R1250.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 1250.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'FR'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.15 Shipping cost per line: region GB (R1150.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 1150.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'GB'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.7.16 Shipping cost per line: region US (R1450.00)
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + 1450.00
      FROM #Orders AS o
     WHERE o.RegionCode = 'US'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');

    -- 8.4 Margin
    UPDATE #Orders
       SET MarginZAR = NetAmountZAR - ReturnedZAR - ISNULL(CostZAR, 0);

    -- 8.5 Brand royalties reduce the margin
    -- 8.5.1 Royalty: Nova (3.50% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0350, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'NOVA'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.2 Royalty: Zenith (3.00% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0300, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'ZENITH'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.3 Royalty: Kalahari (2.50% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0250, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'KALAHARI'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.4 Royalty: Baobab (2.00% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0200, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'BAOBAB'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.5 Royalty: Umoya (4.00% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0400, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'UMOYA'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.6 Royalty: Protea (1.50% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0150, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'PROTEA'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.7 Royalty: Springbok (3.00% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0300, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'SPRINGBOK'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.8 Royalty: Table Mtn (2.50% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0250, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'TABLE_MTN'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.9 Royalty: Karoo (2.00% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0200, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'KAROO'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.10 Royalty: Impala (3.50% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0350, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'IMPALA'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.11 Royalty: Fynbos (1.00% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0100, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'FYNBOS'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.12 Royalty: Marula (3.00% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0300, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'MARULA'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.13 Royalty: Shongololo (1.50% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0150, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'SHONGOLOLO'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.14 Royalty: Acacia (2.00% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0200, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'ACACIA'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.15 Royalty: Rhino (4.50% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0450, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'RHINO'
       AND o.Quantity > o.ReturnedQty;

    -- 8.5.16 Royalty: Sunbird (2.50% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * 0.0250, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = 'SUNBIRD'
       AND o.Quantity > o.ReturnedQty;

    -- 8.6 Payment provider fees reduce the margin
    -- 8.6.1 Payment provider fee: ZAR (1.20%)
    UPDATE #Orders
       SET MarginZAR = MarginZAR - ROUND(NetAmountZAR * 0.0120, 2)
     WHERE CurrencyCode = 'ZAR'
       AND ChannelCode IN ('ONLINE', 'APP', 'MARKETPLACE');

    -- 8.6.2 Payment provider fee: USD (2.90%)
    UPDATE #Orders
       SET MarginZAR = MarginZAR - ROUND(NetAmountZAR * 0.0290, 2)
     WHERE CurrencyCode = 'USD'
       AND ChannelCode IN ('ONLINE', 'APP', 'MARKETPLACE');

    -- 8.6.3 Payment provider fee: EUR (2.50%)
    UPDATE #Orders
       SET MarginZAR = MarginZAR - ROUND(NetAmountZAR * 0.0250, 2)
     WHERE CurrencyCode = 'EUR'
       AND ChannelCode IN ('ONLINE', 'APP', 'MARKETPLACE');

    -- 8.6.4 Payment provider fee: GBP (2.50%)
    UPDATE #Orders
       SET MarginZAR = MarginZAR - ROUND(NetAmountZAR * 0.0250, 2)
     WHERE CurrencyCode = 'GBP'
       AND ChannelCode IN ('ONLINE', 'APP', 'MARKETPLACE');

    -- 8.6.5 Payment provider fee: NAD (1.50%)
    UPDATE #Orders
       SET MarginZAR = MarginZAR - ROUND(NetAmountZAR * 0.0150, 2)
     WHERE CurrencyCode = 'NAD'
       AND ChannelCode IN ('ONLINE', 'APP', 'MARKETPLACE');

    -- 8.6.6 Payment provider fee: BWP (1.80%)
    UPDATE #Orders
       SET MarginZAR = MarginZAR - ROUND(NetAmountZAR * 0.0180, 2)
     WHERE CurrencyCode = 'BWP'
       AND ChannelCode IN ('ONLINE', 'APP', 'MARKETPLACE');

    -- 8.6.7 Payment provider fee: CHF (2.75%)
    UPDATE #Orders
       SET MarginZAR = MarginZAR - ROUND(NetAmountZAR * 0.0275, 2)
     WHERE CurrencyCode = 'CHF'
       AND ChannelCode IN ('ONLINE', 'APP', 'MARKETPLACE');

    -- 8.6.8 Payment provider fee: AUD (2.90%)
    UPDATE #Orders
       SET MarginZAR = MarginZAR - ROUND(NetAmountZAR * 0.0290, 2)
     WHERE CurrencyCode = 'AUD'
       AND ChannelCode IN ('ONLINE', 'APP', 'MARKETPLACE');

    -- 8.6.9 Payment provider fee: CNY (3.20%)
    UPDATE #Orders
       SET MarginZAR = MarginZAR - ROUND(NetAmountZAR * 0.0320, 2)
     WHERE CurrencyCode = 'CNY'
       AND ChannelCode IN ('ONLINE', 'APP', 'MARKETPLACE');

    -- 8.6.10 Payment provider fee: JPY (3.00%)
    UPDATE #Orders
       SET MarginZAR = MarginZAR - ROUND(NetAmountZAR * 0.0300, 2)
     WHERE CurrencyCode = 'JPY'
       AND ChannelCode IN ('ONLINE', 'APP', 'MARKETPLACE');

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'8 margin', @Rows, SYSDATETIME(), 'OK');

    IF @Debug = 1
    BEGIN
        SELECT N'after margin'        AS Stage,
               o.ChannelCode,
               COUNT(*)          AS Lines,
               SUM(o.CostZAR)        AS CostZAR,
               SUM(o.MarginZAR)      AS MarginZAR
          FROM #Orders AS o
         GROUP BY o.ChannelCode
         ORDER BY o.ChannelCode;
    END;

    /* ==========================================================================================
       9. Commission
       ========================================================================================== */
    -- 9.1 Channel default
    UPDATE o
       SET o.CommissionZAR = ROUND(o.NetAmountZAR * ch.CommissionPct, 2)
      FROM #Orders  AS o
      JOIN #Channel AS ch
        ON ch.ChannelCode = o.ChannelCode;

    -- 9.2 Team rates replace the channel default
    -- 9.2.1 Key-account team on contract channels (3.00% of net)
    UPDATE o
       SET o.CommissionZAR = ROUND(o.NetAmountZAR * 0.0300, 2)
      FROM #Orders AS o
      JOIN ref.SalesRep AS r
        ON r.SalesRepId = o.SalesRepId
     WHERE r.TeamCode = 'KEY_ACCOUNTS'
       AND o.ChannelCode IN ('B2B', 'PARTNER');

    -- 9.2.2 Key-account team on telesales (2.00% of net)
    UPDATE o
       SET o.CommissionZAR = ROUND(o.NetAmountZAR * 0.0200, 2)
      FROM #Orders AS o
      JOIN ref.SalesRep AS r
        ON r.SalesRepId = o.SalesRepId
     WHERE r.TeamCode = 'KEY_ACCOUNTS'
       AND o.ChannelCode IN ('TELESALES');

    -- 9.2.3 Telesales team (1.50% of net)
    UPDATE o
       SET o.CommissionZAR = ROUND(o.NetAmountZAR * 0.0150, 2)
      FROM #Orders AS o
      JOIN ref.SalesRep AS r
        ON r.SalesRepId = o.SalesRepId
     WHERE r.TeamCode = 'TELESALES'
       AND o.ChannelCode IN ('TELESALES');

    -- 9.2.4 Field sales on contracts (2.50% of net)
    UPDATE o
       SET o.CommissionZAR = ROUND(o.NetAmountZAR * 0.0250, 2)
      FROM #Orders AS o
      JOIN ref.SalesRep AS r
        ON r.SalesRepId = o.SalesRepId
     WHERE r.TeamCode = 'FIELD'
       AND o.ChannelCode IN ('B2B');

    -- 9.2.5 Field sales assisting in store (1.00% of net)
    UPDATE o
       SET o.CommissionZAR = ROUND(o.NetAmountZAR * 0.0100, 2)
      FROM #Orders AS o
      JOIN ref.SalesRep AS r
        ON r.SalesRepId = o.SalesRepId
     WHERE r.TeamCode = 'FIELD'
       AND o.ChannelCode IN ('STORE');

    -- 9.2.6 Partner managers (1.25% of net)
    UPDATE o
       SET o.CommissionZAR = ROUND(o.NetAmountZAR * 0.0125, 2)
      FROM #Orders AS o
      JOIN ref.SalesRep AS r
        ON r.SalesRepId = o.SalesRepId
     WHERE r.TeamCode = 'PARTNER_MGMT'
       AND o.ChannelCode IN ('PARTNER');

    -- 9.2.7 Export desk (2.00% of net)
    UPDATE o
       SET o.CommissionZAR = ROUND(o.NetAmountZAR * 0.0200, 2)
      FROM #Orders AS o
      JOIN ref.SalesRep AS r
        ON r.SalesRepId = o.SalesRepId
     WHERE r.TeamCode = 'EXPORT_DESK'
       AND o.ChannelCode IN ('EXPORT');

    -- 9.2.8 E-commerce team (0.50% of net)
    UPDATE o
       SET o.CommissionZAR = ROUND(o.NetAmountZAR * 0.0050, 2)
      FROM #Orders AS o
      JOIN ref.SalesRep AS r
        ON r.SalesRepId = o.SalesRepId
     WHERE r.TeamCode = 'ECOM'
       AND o.ChannelCode IN ('ONLINE', 'APP');

    -- 9.2.9 E-commerce team on marketplaces (0.75% of net)
    UPDATE o
       SET o.CommissionZAR = ROUND(o.NetAmountZAR * 0.0075, 2)
      FROM #Orders AS o
      JOIN ref.SalesRep AS r
        ON r.SalesRepId = o.SalesRepId
     WHERE r.TeamCode = 'ECOM'
       AND o.ChannelCode IN ('MARKETPLACE');

    -- 9.2.10 Store staff (0.50% of net)
    UPDATE o
       SET o.CommissionZAR = ROUND(o.NetAmountZAR * 0.0050, 2)
      FROM #Orders AS o
      JOIN ref.SalesRep AS r
        ON r.SalesRepId = o.SalesRepId
     WHERE r.TeamCode = 'STORES'
       AND o.ChannelCode IN ('STORE');

    -- 9.3 No commission on staff purchases or fully returned lines
    UPDATE #Orders
       SET CommissionZAR = 0
     WHERE Segment = 'STAFF'
        OR ReturnedQty >= Quantity;

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'9 commission', @Rows, SYSDATETIME(), 'OK');

    /* ==========================================================================================
       10. Region summary for the targets dashboard
       ========================================================================================== */
    DELETE FROM etl.RegionSummary
     WHERE PeriodMonth = @PeriodMonth;

    DECLARE region_cursor CURSOR LOCAL FAST_FORWARD FOR
        SELECT r.RegionCode
          FROM @Regions AS r
         ORDER BY r.RegionCode;

    OPEN region_cursor;
    FETCH NEXT FROM region_cursor INTO @Region;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        SELECT @RegionNet = SUM(o.NetAmountZAR - o.ReturnedZAR)
          FROM #Orders AS o
         WHERE o.RegionCode = @Region;

        SELECT @RegionTarget = t.TargetZAR
          FROM ref.RegionTarget AS t
         WHERE t.RegionCode  = @Region
           AND t.PeriodMonth = @PeriodMonth;

        INSERT INTO etl.RegionSummary (BatchId, RegionCode, PeriodMonth, NetAmountZAR, TargetZAR, Attainment)
        VALUES (@BatchId, @Region, @PeriodMonth, ISNULL(@RegionNet, 0), @RegionTarget,
                CASE WHEN @RegionTarget > 0 THEN ISNULL(@RegionNet, 0) / @RegionTarget END);

        FETCH NEXT FROM region_cursor INTO @Region;
    END;

    CLOSE region_cursor;
    DEALLOCATE region_cursor;

    SET @Rows = @@ROWCOUNT;
    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'10 region summary', @Rows, SYSDATETIME(), 'OK');

    /* ==========================================================================================
       11. Data-quality checks
       ========================================================================================== */
    -- 11.1 NEGATIVE_NET
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'NEGATIVE_NET', o.OrderId, o.LineNumber, 'NetAmountZAR',
           CONCAT(N'Net amount ', o.NetAmountZAR, N' is below zero'),
           'HIGH', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.NetAmountZAR < 0;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.2 ZERO_QUANTITY
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'ZERO_QUANTITY', o.OrderId, o.LineNumber, 'Quantity',
           CONCAT(N'Quantity ', o.Quantity, N' is not positive'),
           'HIGH', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.Quantity <= 0;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.3 MISSING_FX
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'MISSING_FX', o.OrderId, o.LineNumber, 'FxRate',
           CONCAT(N'No exchange rate for ', o.CurrencyCode, N' on ', o.OrderDate),
           'HIGH', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.FxRate IS NULL;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.4 DISCOUNT_OVER_GROSS
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'DISCOUNT_OVER_GROSS', o.OrderId, o.LineNumber, 'DiscountAmount',
           N'Discount exceeds the gross amount',
           'HIGH', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.DiscountAmount > o.GrossAmount;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.5 NEGATIVE_MARGIN
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'NEGATIVE_MARGIN', o.OrderId, o.LineNumber, 'MarginZAR',
           CONCAT(N'Margin ', o.MarginZAR, N' is negative'),
           'MEDIUM', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.MarginZAR < 0 AND o.Segment <> 'STAFF';

    SET @DqIssues += @@ROWCOUNT;

    -- 11.6 MARGIN_OVER_NET
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'MARGIN_OVER_NET', o.OrderId, o.LineNumber, 'MarginZAR',
           N'Margin is larger than the net amount',
           'HIGH', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.MarginZAR > o.NetAmountZAR;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.7 MISSING_COST
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'MISSING_COST', o.OrderId, o.LineNumber, 'CostZAR',
           CONCAT(N'No cost for product ', o.ProductId),
           'MEDIUM', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.CostZAR IS NULL;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.8 UNKNOWN_PRODUCT
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'UNKNOWN_PRODUCT', o.OrderId, o.LineNumber, 'ProductId',
           CONCAT(N'Product ', o.ProductId, N' is not active or unknown'),
           'MEDIUM', SYSDATETIME()
      FROM #Orders AS o
     WHERE NOT EXISTS (SELECT 1 FROM #Product AS p WHERE p.ProductId = o.ProductId);

    SET @DqIssues += @@ROWCOUNT;

    -- 11.9 UNKNOWN_CHANNEL
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'UNKNOWN_CHANNEL', o.OrderId, o.LineNumber, 'ChannelCode',
           CONCAT(N'Channel ', o.ChannelCode, N' is unknown'),
           'MEDIUM', SYSDATETIME()
      FROM #Orders AS o
     WHERE NOT EXISTS (SELECT 1 FROM #Channel AS c WHERE c.ChannelCode = o.ChannelCode);

    SET @DqIssues += @@ROWCOUNT;

    -- 11.10 UNKNOWN_REGION
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'UNKNOWN_REGION', o.OrderId, o.LineNumber, 'RegionCode',
           N'Region could not be derived',
           'MEDIUM', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.RegionCode IS NULL;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.11 RETURN_OVER_SALE
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'RETURN_OVER_SALE', o.OrderId, o.LineNumber, 'ReturnedQty',
           CONCAT(N'Returned ', o.ReturnedQty, N' of ', o.Quantity),
           'HIGH', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.ReturnedQty > o.Quantity;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.12 REFUND_OVER_NET
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'REFUND_OVER_NET', o.OrderId, o.LineNumber, 'ReturnedZAR',
           N'Refund exceeds the net amount',
           'MEDIUM', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.ReturnedZAR > o.NetAmountZAR;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.13 TAX_RATE_UNUSUAL
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'TAX_RATE_UNUSUAL', o.OrderId, o.LineNumber, 'TaxRate',
           CONCAT(N'Tax rate ', o.TaxRate, N' is not 0% or 15%'),
           'LOW', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.TaxRate NOT IN (0.0000, 0.1500);

    SET @DqIssues += @@ROWCOUNT;

    -- 11.14 TAX_ON_EXPORT
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'TAX_ON_EXPORT', o.OrderId, o.LineNumber, 'TaxZAR',
           N'Export order carries VAT',
           'MEDIUM', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.TaxZAR <> 0 AND o.ChannelCode = 'EXPORT';

    SET @DqIssues += @@ROWCOUNT;

    -- 11.15 SHIP_BEFORE_ORDER
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'SHIP_BEFORE_ORDER', o.OrderId, o.LineNumber, 'ShipDate',
           CONCAT(N'Shipped ', o.ShipDate, N' before ordered ', o.OrderDate),
           'LOW', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.ShipDate < o.OrderDate;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.16 LATE_SHIPMENT
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'LATE_SHIPMENT', o.OrderId, o.LineNumber, 'ShipDate',
           N'Shipped more than 60 days after the order',
           'LOW', SYSDATETIME()
      FROM #Orders AS o
     WHERE DATEDIFF(DAY, o.OrderDate, o.ShipDate) > 60;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.17 COMMISSION_OVER_MARGIN
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'COMMISSION_OVER_MARGIN', o.OrderId, o.LineNumber, 'CommissionZAR',
           N'Commission exceeds the margin',
           'MEDIUM', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.CommissionZAR > o.MarginZAR AND o.MarginZAR > 0;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.18 PROMO_EXPIRED
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'PROMO_EXPIRED', o.OrderId, o.LineNumber, 'PromoCode',
           CONCAT(N'Promotion ', o.PromoCode, N' gave no discount'),
           'LOW', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.PromoCode IS NOT NULL AND o.DiscountAmount = 0;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.19 HIGH_UNIT_PRICE
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'HIGH_UNIT_PRICE', o.OrderId, o.LineNumber, 'UnitPrice',
           CONCAT(N'Unit price ', o.UnitPrice, N' looks too high'),
           'LOW', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.UnitPrice > 500000;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.20 NO_SALES_REP
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'NO_SALES_REP', o.OrderId, o.LineNumber, 'SalesRepId',
           N'Contract or telesales order without a sales rep',
           'LOW', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.SalesRepId IS NULL AND o.ChannelCode IN ('B2B', 'TELESALES');

    SET @DqIssues += @@ROWCOUNT;

    -- 11.21 INTERNAL_CUSTOMER
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'INTERNAL_CUSTOMER', o.OrderId, o.LineNumber, 'IsInternal',
           N'Internal customer order reached the fact load',
           'HIGH', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.IsInternal = 1;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.22 ADJUSTED_NO_NET_CHANGE
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'ADJUSTED_NO_NET_CHANGE', o.OrderId, o.LineNumber, 'IsAdjusted',
           N'Adjustment did not change the net amount',
           'LOW', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.IsAdjusted = 1 AND o.NetAmountZAR = o.GrossAmountZAR - o.DiscountZAR;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.23 FX_RATE_OUTLIER
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'FX_RATE_OUTLIER', o.OrderId, o.LineNumber, 'FxRate',
           CONCAT(N'Exchange rate ', o.FxRate, N' is outside the expected range'),
           'MEDIUM', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.FxRate > 100 OR o.FxRate < 0.001;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.24 MISSING_DATE_KEY
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'MISSING_DATE_KEY', o.OrderId, o.LineNumber, 'DateKey',
           N'Order date is not in the calendar',
           'HIGH', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.DateKey IS NULL;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.25 WEEKEND_B2B
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'WEEKEND_B2B', o.OrderId, o.LineNumber, 'OrderDate',
           N'Contract order dated on a weekend',
           'LOW', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.ChannelCode = 'B2B' AND DATEPART(WEEKDAY, o.OrderDate) IN (1, 7);

    SET @DqIssues += @@ROWCOUNT;

    -- 11.26 ZERO_PRICE
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'ZERO_PRICE', o.OrderId, o.LineNumber, 'UnitPrice',
           N'Zero unit price outside staff purchases',
           'MEDIUM', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.UnitPrice = 0 AND o.Segment <> 'STAFF';

    SET @DqIssues += @@ROWCOUNT;

    -- 11.27 NEGATIVE_COST
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'NEGATIVE_COST', o.OrderId, o.LineNumber, 'CostZAR',
           CONCAT(N'Cost ', o.CostZAR, N' is negative'),
           'HIGH', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.CostZAR < 0;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.28 TAX_MISSING
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'TAX_MISSING', o.OrderId, o.LineNumber, 'TaxRate',
           CONCAT(N'No tax rate for region ', o.RegionCode),
           'MEDIUM', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.TaxRate IS NULL;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.29 CURRENCY_REGION_MISMATCH
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'CURRENCY_REGION_MISMATCH', o.OrderId, o.LineNumber, 'CurrencyCode',
           N'Export region invoiced in rand',
           'LOW', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.CurrencyCode = 'ZAR' AND o.RegionCode IN ('DE', 'NL', 'FR', 'GB', 'US');

    SET @DqIssues += @@ROWCOUNT;

    -- 11.30 DISCOUNT_ON_STAFF_PROMO
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'DISCOUNT_ON_STAFF_PROMO', o.OrderId, o.LineNumber, 'PromoCode',
           N'Promotion used on a staff purchase',
           'LOW', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.Segment = 'STAFF' AND o.PromoCode IS NOT NULL;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.31 HUGE_LINE
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'HUGE_LINE', o.OrderId, o.LineNumber, 'NetAmountZAR',
           CONCAT(N'Line worth R', o.NetAmountZAR),
           'MEDIUM', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.NetAmountZAR > 5000000;

    SET @DqIssues += @@ROWCOUNT;

    -- 11.32 ADJUSTED_EXPORT
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, 'ADJUSTED_EXPORT', o.OrderId, o.LineNumber, 'IsAdjusted',
           N'Manual adjustment on an export order',
           'LOW', SYSDATETIME()
      FROM #Orders AS o
     WHERE o.IsAdjusted = 1 AND o.ChannelCode = 'EXPORT';

    SET @DqIssues += @@ROWCOUNT;

    INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)
    VALUES (@ProcName, @BatchId, N'11 data quality', @DqIssues, SYSDATETIME(),
            CASE WHEN @DqIssues > 0 THEN 'WARNING' ELSE 'OK' END);

    /* ==========================================================================================
       12. Publish to dbo.FactRevenue
       ========================================================================================== */
    BEGIN TRY
        BEGIN TRANSACTION;

        IF @Mode IN ('FULL', 'RESTATE')
        BEGIN
            -- restatement replaces the whole window
            DELETE f
              FROM dbo.FactRevenue AS f
              JOIN #Calendar AS cal
                ON cal.DateKey = f.DateKey;
        END;

        MERGE dbo.FactRevenue AS tgt
        USING (SELECT o.OrderId,
                      o.LineNumber,
                      o.DateKey,
                      o.CustomerId,
                      o.ProductId,
                      o.ChannelCode,
                      o.RegionCode,
                      o.Quantity,
                      o.GrossAmountZAR,
                      o.DiscountZAR,
                      o.NetAmountZAR,
                      o.TaxZAR,
                      o.CostZAR,
                      o.MarginZAR,
                      o.ReturnedZAR,
                      o.CommissionZAR,
                      o.PromoCode,
                      o.IsAdjusted,
                      HASHBYTES('SHA2_256',
                                CONCAT(o.DateKey, '|', o.CustomerId, '|', o.ProductId, '|', o.ChannelCode, '|',
                                       o.Quantity, '|', o.NetAmountZAR, '|', o.TaxZAR, '|', o.CostZAR, '|',
                                       o.ReturnedZAR, '|', o.CommissionZAR)) AS RowHash
                 FROM #Orders AS o) AS src
           ON tgt.OrderId    = src.OrderId
          AND tgt.LineNumber = src.LineNumber
        WHEN MATCHED AND tgt.RowHash <> src.RowHash THEN
            UPDATE SET tgt.DateKey        = src.DateKey,
                       tgt.CustomerId     = src.CustomerId,
                       tgt.ProductId      = src.ProductId,
                       tgt.ChannelCode    = src.ChannelCode,
                       tgt.RegionCode     = src.RegionCode,
                       tgt.Quantity       = src.Quantity,
                       tgt.GrossAmountZAR = src.GrossAmountZAR,
                       tgt.DiscountZAR    = src.DiscountZAR,
                       tgt.NetAmountZAR   = src.NetAmountZAR,
                       tgt.TaxZAR         = src.TaxZAR,
                       tgt.CostZAR        = src.CostZAR,
                       tgt.MarginZAR      = src.MarginZAR,
                       tgt.ReturnedZAR    = src.ReturnedZAR,
                       tgt.CommissionZAR  = src.CommissionZAR,
                       tgt.PromoCode      = src.PromoCode,
                       tgt.IsAdjusted     = src.IsAdjusted,
                       tgt.RowHash        = src.RowHash,
                       tgt.LoadBatchId    = @BatchId,
                       tgt.LoadedAt       = SYSDATETIME()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (OrderId, LineNumber, DateKey, CustomerId, ProductId, ChannelCode, RegionCode, Quantity,
                    GrossAmountZAR, DiscountZAR, NetAmountZAR, TaxZAR, CostZAR, MarginZAR, ReturnedZAR,
                    CommissionZAR, PromoCode, IsAdjusted, RowHash, LoadBatchId, LoadedAt)
            VALUES (src.OrderId, src.LineNumber, src.DateKey, src.CustomerId, src.ProductId, src.ChannelCode,
                    src.RegionCode, src.Quantity, src.GrossAmountZAR, src.DiscountZAR, src.NetAmountZAR,
                    src.TaxZAR, src.CostZAR, src.MarginZAR, src.ReturnedZAR, src.CommissionZAR, src.PromoCode,
                    src.IsAdjusted, src.RowHash, @BatchId, SYSDATETIME())
        OUTPUT @BatchId, $action, inserted.OrderId, inserted.LineNumber, deleted.NetAmountZAR,
               inserted.NetAmountZAR, SYSDATETIME()
          INTO etl.FactRevenueAudit (BatchId, Action, OrderId, LineNumber, OldNetAmountZAR, NewNetAmountZAR, ChangedAt);

        SET @RowsLoaded = @@ROWCOUNT;

        UPDATE etl.LoadBatch
           SET Status     = 'SUCCEEDED',
               FinishedAt = SYSDATETIME(),
               RowsLoaded = @RowsLoaded,
               DqIssues   = @DqIssues
         WHERE BatchId = @BatchId;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;

        SET @Msg = ERROR_MESSAGE();

        INSERT INTO etl.ErrorLog (ProcName, BatchId, ErrorNumber, ErrorSeverity, ErrorState, ErrorLine, ErrorMessage, LoggedAt)
        VALUES (@ProcName, @BatchId, ERROR_NUMBER(), ERROR_SEVERITY(), ERROR_STATE(), ERROR_LINE(), @Msg, SYSDATETIME());

        UPDATE etl.LoadBatch
           SET Status     = 'FAILED',
               FinishedAt = SYSDATETIME()
         WHERE BatchId = @BatchId;

        THROW;
    END CATCH;

    /* ==========================================================================================
       12b. Reconciliation: what was staged against what the fact table now holds
       ========================================================================================== */
    -- 12.2.1 GrossAmountZAR: staged against loaded for the window
    INSERT INTO etl.Reconciliation (BatchId, Measure, StagedTotal, LoadedTotal, Difference, CheckedAt)
    SELECT @BatchId,
           'GrossAmountZAR',
           s.StagedTotal,
           f.LoadedTotal,
           ISNULL(s.StagedTotal, 0) - ISNULL(f.LoadedTotal, 0),
           SYSDATETIME()
      FROM (SELECT SUM(o.GrossAmountZAR) AS StagedTotal
              FROM #Orders AS o) AS s
     CROSS JOIN (SELECT SUM(fr.GrossAmountZAR) AS LoadedTotal
                   FROM dbo.FactRevenue AS fr
                   JOIN #Calendar AS cal
                     ON cal.DateKey = fr.DateKey) AS f;

    -- 12.2.2 DiscountZAR: staged against loaded for the window
    INSERT INTO etl.Reconciliation (BatchId, Measure, StagedTotal, LoadedTotal, Difference, CheckedAt)
    SELECT @BatchId,
           'DiscountZAR',
           s.StagedTotal,
           f.LoadedTotal,
           ISNULL(s.StagedTotal, 0) - ISNULL(f.LoadedTotal, 0),
           SYSDATETIME()
      FROM (SELECT SUM(o.DiscountZAR) AS StagedTotal
              FROM #Orders AS o) AS s
     CROSS JOIN (SELECT SUM(fr.DiscountZAR) AS LoadedTotal
                   FROM dbo.FactRevenue AS fr
                   JOIN #Calendar AS cal
                     ON cal.DateKey = fr.DateKey) AS f;

    -- 12.2.3 NetAmountZAR: staged against loaded for the window
    INSERT INTO etl.Reconciliation (BatchId, Measure, StagedTotal, LoadedTotal, Difference, CheckedAt)
    SELECT @BatchId,
           'NetAmountZAR',
           s.StagedTotal,
           f.LoadedTotal,
           ISNULL(s.StagedTotal, 0) - ISNULL(f.LoadedTotal, 0),
           SYSDATETIME()
      FROM (SELECT SUM(o.NetAmountZAR) AS StagedTotal
              FROM #Orders AS o) AS s
     CROSS JOIN (SELECT SUM(fr.NetAmountZAR) AS LoadedTotal
                   FROM dbo.FactRevenue AS fr
                   JOIN #Calendar AS cal
                     ON cal.DateKey = fr.DateKey) AS f;

    -- 12.2.4 TaxZAR: staged against loaded for the window
    INSERT INTO etl.Reconciliation (BatchId, Measure, StagedTotal, LoadedTotal, Difference, CheckedAt)
    SELECT @BatchId,
           'TaxZAR',
           s.StagedTotal,
           f.LoadedTotal,
           ISNULL(s.StagedTotal, 0) - ISNULL(f.LoadedTotal, 0),
           SYSDATETIME()
      FROM (SELECT SUM(o.TaxZAR) AS StagedTotal
              FROM #Orders AS o) AS s
     CROSS JOIN (SELECT SUM(fr.TaxZAR) AS LoadedTotal
                   FROM dbo.FactRevenue AS fr
                   JOIN #Calendar AS cal
                     ON cal.DateKey = fr.DateKey) AS f;

    -- 12.2.5 CostZAR: staged against loaded for the window
    INSERT INTO etl.Reconciliation (BatchId, Measure, StagedTotal, LoadedTotal, Difference, CheckedAt)
    SELECT @BatchId,
           'CostZAR',
           s.StagedTotal,
           f.LoadedTotal,
           ISNULL(s.StagedTotal, 0) - ISNULL(f.LoadedTotal, 0),
           SYSDATETIME()
      FROM (SELECT SUM(o.CostZAR) AS StagedTotal
              FROM #Orders AS o) AS s
     CROSS JOIN (SELECT SUM(fr.CostZAR) AS LoadedTotal
                   FROM dbo.FactRevenue AS fr
                   JOIN #Calendar AS cal
                     ON cal.DateKey = fr.DateKey) AS f;

    -- 12.2.6 MarginZAR: staged against loaded for the window
    INSERT INTO etl.Reconciliation (BatchId, Measure, StagedTotal, LoadedTotal, Difference, CheckedAt)
    SELECT @BatchId,
           'MarginZAR',
           s.StagedTotal,
           f.LoadedTotal,
           ISNULL(s.StagedTotal, 0) - ISNULL(f.LoadedTotal, 0),
           SYSDATETIME()
      FROM (SELECT SUM(o.MarginZAR) AS StagedTotal
              FROM #Orders AS o) AS s
     CROSS JOIN (SELECT SUM(fr.MarginZAR) AS LoadedTotal
                   FROM dbo.FactRevenue AS fr
                   JOIN #Calendar AS cal
                     ON cal.DateKey = fr.DateKey) AS f;

    -- 12.2.7 ReturnedZAR: staged against loaded for the window
    INSERT INTO etl.Reconciliation (BatchId, Measure, StagedTotal, LoadedTotal, Difference, CheckedAt)
    SELECT @BatchId,
           'ReturnedZAR',
           s.StagedTotal,
           f.LoadedTotal,
           ISNULL(s.StagedTotal, 0) - ISNULL(f.LoadedTotal, 0),
           SYSDATETIME()
      FROM (SELECT SUM(o.ReturnedZAR) AS StagedTotal
              FROM #Orders AS o) AS s
     CROSS JOIN (SELECT SUM(fr.ReturnedZAR) AS LoadedTotal
                   FROM dbo.FactRevenue AS fr
                   JOIN #Calendar AS cal
                     ON cal.DateKey = fr.DateKey) AS f;

    -- 12.2.8 CommissionZAR: staged against loaded for the window
    INSERT INTO etl.Reconciliation (BatchId, Measure, StagedTotal, LoadedTotal, Difference, CheckedAt)
    SELECT @BatchId,
           'CommissionZAR',
           s.StagedTotal,
           f.LoadedTotal,
           ISNULL(s.StagedTotal, 0) - ISNULL(f.LoadedTotal, 0),
           SYSDATETIME()
      FROM (SELECT SUM(o.CommissionZAR) AS StagedTotal
              FROM #Orders AS o) AS s
     CROSS JOIN (SELECT SUM(fr.CommissionZAR) AS LoadedTotal
                   FROM dbo.FactRevenue AS fr
                   JOIN #Calendar AS cal
                     ON cal.DateKey = fr.DateKey) AS f;

    /* ==========================================================================================
       13. Debug output
       ========================================================================================== */
    IF @Debug = 1
    BEGIN
        SELECT o.RegionCode,
               o.ChannelCode,
               COUNT(*)              AS Lines,
               SUM(o.NetAmountZAR)   AS NetAmountZAR,
               SUM(o.MarginZAR)      AS MarginZAR,
               SUM(o.CommissionZAR)  AS CommissionZAR
          FROM #Orders AS o
         GROUP BY o.RegionCode, o.ChannelCode
         ORDER BY o.RegionCode, o.ChannelCode;
    END;

    RETURN 0;
END
GO
