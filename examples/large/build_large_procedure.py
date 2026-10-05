#!/usr/bin/env python3
"""Writes the large demo procedure (etl.usp_LoadFactRevenue) and the schema it runs against.

The procedure is synthetic but built the way long ETL procedures grow in practice: staged
temp tables, many rule blocks added over the years, data-quality checks, a cursor, dynamic SQL,
a transaction around the final MERGE. One bug is planted on purpose (see README.md): for orders
with an approved PRICE adjustment, step "7.2" multiplies NetAmountZAR by the FX rate a second
time, so those rows come out wrong by the exchange rate (about 20x for EUR orders).

    python examples/large/build_large_procedure.py      # rewrites the two .sql files next to it
"""
from pathlib import Path

HERE = Path(__file__).parent

CHANNEL_DISCOUNTS = [
    ("ONLINE", "CONSUMER", "0.0200", "Web shop loyalty discount"),
    ("ONLINE", "SMB", "0.0150", "Web shop small-business discount"),
    ("APP", "CONSUMER", "0.0300", "Mobile app discount"),
    ("APP", "SMB", "0.0100", "Mobile app small-business discount"),
    ("STORE", "STAFF", "0.1000", "Staff purchase discount"),
    ("STORE", "CONSUMER", "0.0050", "In-store card discount"),
    ("PARTNER", "ENTERPRISE", "0.0500", "Partner rebate"),
    ("PARTNER", "SMB", "0.0350", "Partner small-business rebate"),
    ("TELESALES", "ENTERPRISE", "0.0250", "Telesales volume discount"),
    ("TELESALES", "SMB", "0.0100", "Telesales small-business discount"),
    ("B2B", "ENTERPRISE", "0.0400", "Contract discount"),
    ("B2B", "GOVERNMENT", "0.0600", "Tender discount"),
    ("EXPORT", "ENTERPRISE", "0.0200", "Export incentive"),
    ("MARKETPLACE", "CONSUMER", "0.0000", "Marketplace orders (fees are booked separately)"),
]
REGION_TAX = [
    ("GP", "Gauteng", "0.1500"), ("WC", "Western Cape", "0.1500"), ("KN", "KwaZulu-Natal", "0.1500"),
    ("EC", "Eastern Cape", "0.1500"), ("FS", "Free State", "0.1500"), ("LP", "Limpopo", "0.1500"),
    ("MP", "Mpumalanga", "0.1500"), ("NW", "North West", "0.1500"), ("NC", "Northern Cape", "0.1500"),
    ("NA", "Namibia (export)", "0.0000"), ("BW", "Botswana (export)", "0.0000"), ("DE", "Germany (export)", "0.0000"),
    ("NL", "Netherlands (export)", "0.0000"), ("FR", "France (export)", "0.0000"), ("GB", "United Kingdom (export)", "0.0000"),
    ("US", "United States (export)", "0.0000"),
]
CATEGORY_UPLIFT = [
    ("APPLIANCE", "Appliances", "0.0450"), ("TV", "Televisions", "0.0600"), ("AUDIO", "Audio", "0.0250"),
    ("PHONE", "Phones", "0.0150"), ("LAPTOP", "Laptops", "0.0200"), ("TABLET", "Tablets", "0.0150"),
    ("GAMING", "Gaming", "0.0200"), ("CAMERA", "Cameras", "0.0200"), ("SMARTHOME", "Smart home", "0.0300"),
    ("FURNITURE", "Furniture", "0.0800"), ("GARDEN", "Garden", "0.0700"), ("TOOLS", "Tools", "0.0350"),
    ("SPORT", "Sport", "0.0300"), ("TOYS", "Toys", "0.0250"), ("BOOKS", "Books", "0.0100"),
    ("GROCERY", "Grocery", "0.0500"), ("HEALTH", "Health", "0.0200"), ("ACCESSORY", "Accessories", "0.0100"),
]
COMMISSION = [
    ("KEY_ACCOUNTS", "'B2B', 'PARTNER'", "0.0300", "Key-account team on contract channels"),
    ("KEY_ACCOUNTS", "'TELESALES'", "0.0200", "Key-account team on telesales"),
    ("TELESALES", "'TELESALES'", "0.0150", "Telesales team"),
    ("FIELD", "'B2B'", "0.0250", "Field sales on contracts"),
    ("FIELD", "'STORE'", "0.0100", "Field sales assisting in store"),
    ("PARTNER_MGMT", "'PARTNER'", "0.0125", "Partner managers"),
    ("EXPORT_DESK", "'EXPORT'", "0.0200", "Export desk"),
    ("ECOM", "'ONLINE', 'APP'", "0.0050", "E-commerce team"),
    ("ECOM", "'MARKETPLACE'", "0.0075", "E-commerce team on marketplaces"),
    ("STORES", "'STORE'", "0.0050", "Store staff"),
]
BRAND_ROYALTIES = [
    ("NOVA", "0.0350"), ("ZENITH", "0.0300"), ("KALAHARI", "0.0250"), ("BAOBAB", "0.0200"), ("UMOYA", "0.0400"),
    ("PROTEA", "0.0150"), ("SPRINGBOK", "0.0300"), ("TABLE_MTN", "0.0250"), ("KAROO", "0.0200"), ("IMPALA", "0.0350"),
    ("FYNBOS", "0.0100"), ("MARULA", "0.0300"), ("SHONGOLOLO", "0.0150"), ("ACACIA", "0.0200"), ("RHINO", "0.0450"),
    ("SUNBIRD", "0.0250"),
]
PAYMENT_FEES = [
    ("ZAR", "0.0120"), ("USD", "0.0290"), ("EUR", "0.0250"), ("GBP", "0.0250"), ("NAD", "0.0150"),
    ("BWP", "0.0180"), ("CHF", "0.0275"), ("AUD", "0.0290"), ("CNY", "0.0320"), ("JPY", "0.0300"),
]
SHIPPING = [
    ("GP", "45.00"), ("WC", "65.00"), ("KN", "60.00"), ("EC", "75.00"), ("FS", "70.00"), ("LP", "80.00"),
    ("MP", "70.00"), ("NW", "70.00"), ("NC", "95.00"), ("NA", "350.00"), ("BW", "300.00"), ("DE", "1250.00"),
    ("NL", "1250.00"), ("FR", "1250.00"), ("GB", "1150.00"), ("US", "1450.00"),
]
RECONCILE = ["GrossAmountZAR", "DiscountZAR", "NetAmountZAR", "TaxZAR", "CostZAR", "MarginZAR", "ReturnedZAR",
             "CommissionZAR"]
DQ_RULES = [
    ("NEGATIVE_NET", "NetAmountZAR", "o.NetAmountZAR < 0", "CONCAT(N'Net amount ', o.NetAmountZAR, N' is below zero')", "HIGH"),
    ("ZERO_QUANTITY", "Quantity", "o.Quantity <= 0", "CONCAT(N'Quantity ', o.Quantity, N' is not positive')", "HIGH"),
    ("MISSING_FX", "FxRate", "o.FxRate IS NULL", "CONCAT(N'No exchange rate for ', o.CurrencyCode, N' on ', o.OrderDate)", "HIGH"),
    ("DISCOUNT_OVER_GROSS", "DiscountAmount", "o.DiscountAmount > o.GrossAmount", "N'Discount exceeds the gross amount'", "HIGH"),
    ("NEGATIVE_MARGIN", "MarginZAR", "o.MarginZAR < 0 AND o.Segment <> 'STAFF'", "CONCAT(N'Margin ', o.MarginZAR, N' is negative')", "MEDIUM"),
    ("MARGIN_OVER_NET", "MarginZAR", "o.MarginZAR > o.NetAmountZAR", "N'Margin is larger than the net amount'", "HIGH"),
    ("MISSING_COST", "CostZAR", "o.CostZAR IS NULL", "CONCAT(N'No cost for product ', o.ProductId)", "MEDIUM"),
    ("UNKNOWN_PRODUCT", "ProductId", "NOT EXISTS (SELECT 1 FROM #Product AS p WHERE p.ProductId = o.ProductId)", "CONCAT(N'Product ', o.ProductId, N' is not active or unknown')", "MEDIUM"),
    ("UNKNOWN_CHANNEL", "ChannelCode", "NOT EXISTS (SELECT 1 FROM #Channel AS c WHERE c.ChannelCode = o.ChannelCode)", "CONCAT(N'Channel ', o.ChannelCode, N' is unknown')", "MEDIUM"),
    ("UNKNOWN_REGION", "RegionCode", "o.RegionCode IS NULL", "N'Region could not be derived'", "MEDIUM"),
    ("RETURN_OVER_SALE", "ReturnedQty", "o.ReturnedQty > o.Quantity", "CONCAT(N'Returned ', o.ReturnedQty, N' of ', o.Quantity)", "HIGH"),
    ("REFUND_OVER_NET", "ReturnedZAR", "o.ReturnedZAR > o.NetAmountZAR", "N'Refund exceeds the net amount'", "MEDIUM"),
    ("TAX_RATE_UNUSUAL", "TaxRate", "o.TaxRate NOT IN (0.0000, 0.1500)", "CONCAT(N'Tax rate ', o.TaxRate, N' is not 0% or 15%')", "LOW"),
    ("TAX_ON_EXPORT", "TaxZAR", "o.TaxZAR <> 0 AND o.ChannelCode = 'EXPORT'", "N'Export order carries VAT'", "MEDIUM"),
    ("SHIP_BEFORE_ORDER", "ShipDate", "o.ShipDate < o.OrderDate", "CONCAT(N'Shipped ', o.ShipDate, N' before ordered ', o.OrderDate)", "LOW"),
    ("LATE_SHIPMENT", "ShipDate", "DATEDIFF(DAY, o.OrderDate, o.ShipDate) > 60", "N'Shipped more than 60 days after the order'", "LOW"),
    ("COMMISSION_OVER_MARGIN", "CommissionZAR", "o.CommissionZAR > o.MarginZAR AND o.MarginZAR > 0", "N'Commission exceeds the margin'", "MEDIUM"),
    ("PROMO_EXPIRED", "PromoCode", "o.PromoCode IS NOT NULL AND o.DiscountAmount = 0", "CONCAT(N'Promotion ', o.PromoCode, N' gave no discount')", "LOW"),
    ("HIGH_UNIT_PRICE", "UnitPrice", "o.UnitPrice > 500000", "CONCAT(N'Unit price ', o.UnitPrice, N' looks too high')", "LOW"),
    ("NO_SALES_REP", "SalesRepId", "o.SalesRepId IS NULL AND o.ChannelCode IN ('B2B', 'TELESALES')", "N'Contract or telesales order without a sales rep'", "LOW"),
    ("INTERNAL_CUSTOMER", "IsInternal", "o.IsInternal = 1", "N'Internal customer order reached the fact load'", "HIGH"),
    ("ADJUSTED_NO_NET_CHANGE", "IsAdjusted", "o.IsAdjusted = 1 AND o.NetAmountZAR = o.GrossAmountZAR - o.DiscountZAR", "N'Adjustment did not change the net amount'", "LOW"),
    ("FX_RATE_OUTLIER", "FxRate", "o.FxRate > 100 OR o.FxRate < 0.001", "CONCAT(N'Exchange rate ', o.FxRate, N' is outside the expected range')", "MEDIUM"),
    ("MISSING_DATE_KEY", "DateKey", "o.DateKey IS NULL", "N'Order date is not in the calendar'", "HIGH"),
    ("WEEKEND_B2B", "OrderDate", "o.ChannelCode = 'B2B' AND DATEPART(WEEKDAY, o.OrderDate) IN (1, 7)", "N'Contract order dated on a weekend'", "LOW"),
    ("ZERO_PRICE", "UnitPrice", "o.UnitPrice = 0 AND o.Segment <> 'STAFF'", "N'Zero unit price outside staff purchases'", "MEDIUM"),
    ("NEGATIVE_COST", "CostZAR", "o.CostZAR < 0", "CONCAT(N'Cost ', o.CostZAR, N' is negative')", "HIGH"),
    ("TAX_MISSING", "TaxRate", "o.TaxRate IS NULL", "CONCAT(N'No tax rate for region ', o.RegionCode)", "MEDIUM"),
    ("CURRENCY_REGION_MISMATCH", "CurrencyCode", "o.CurrencyCode = 'ZAR' AND o.RegionCode IN ('DE', 'NL', 'FR', 'GB', 'US')", "N'Export region invoiced in rand'", "LOW"),
    ("DISCOUNT_ON_STAFF_PROMO", "PromoCode", "o.Segment = 'STAFF' AND o.PromoCode IS NOT NULL", "N'Promotion used on a staff purchase'", "LOW"),
    ("HUGE_LINE", "NetAmountZAR", "o.NetAmountZAR > 5000000", "CONCAT(N'Line worth R', o.NetAmountZAR)", "MEDIUM"),
    ("ADJUSTED_EXPORT", "IsAdjusted", "o.IsAdjusted = 1 AND o.ChannelCode = 'EXPORT'", "N'Manual adjustment on an export order'", "LOW"),
]


def channel_blocks():
    out = []
    for i, (ch, seg, pct, label) in enumerate(CHANNEL_DISCOUNTS, 1):
        out.append(f"""    -- 4.3.{i} {label} ({ch} / {seg}, {float(pct) * 100:.1f}%)
    UPDATE o
       SET o.DiscountAmount = o.DiscountAmount + ROUND(o.GrossAmount * {pct}, 2)
      FROM #Orders AS o
     WHERE o.ChannelCode = '{ch}'
       AND o.Segment = '{seg}'
       AND o.PromoCode IS NULL;          -- promotions and channel discounts do not stack

    SET @Rows = @@ROWCOUNT;
    IF @Debug = 1 PRINT CONCAT(N'4.3.{i} {ch}/{seg}: ', @Rows, N' line(s)');
""")
    return "\n".join(out)


def tax_blocks():
    out = []
    for i, (code, name, rate) in enumerate(REGION_TAX, 1):
        out.append(f"""    -- 4.6.{i} {name}
    UPDATE #Orders
       SET TaxRate = {rate}
     WHERE RegionCode = '{code}'
       AND TaxRate IS NULL;
""")
    return "\n".join(out)


def uplift_blocks():
    out = []
    for i, (cat, name, pct) in enumerate(CATEGORY_UPLIFT, 1):
        out.append(f"""    -- 8.3.{i} Handling uplift: {name} ({float(pct) * 100:.2f}%)
    UPDATE o
       SET o.CostZAR = o.CostZAR + ROUND(o.CostZAR * {pct}, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.CategoryCode = '{cat}'
       AND o.CostZAR IS NOT NULL;
""")
    return "\n".join(out)


def commission_blocks():
    out = []
    for i, (team, channels, pct, label) in enumerate(COMMISSION, 1):
        out.append(f"""    -- 9.2.{i} {label} ({float(pct) * 100:.2f}% of net)
    UPDATE o
       SET o.CommissionZAR = ROUND(o.NetAmountZAR * {pct}, 2)
      FROM #Orders AS o
      JOIN ref.SalesRep AS r
        ON r.SalesRepId = o.SalesRepId
     WHERE r.TeamCode = '{team}'
       AND o.ChannelCode IN ({channels});
""")
    return "\n".join(out)


def dq_blocks():
    out = []
    for i, (code, col, cond, detail, sev) in enumerate(DQ_RULES, 1):
        out.append(f"""    -- 11.{i} {code}
    INSERT INTO etl.DataQualityIssue
        (BatchId, RuleCode, OrderId, LineNumber, ColumnName, Detail, Severity, DetectedAt)
    SELECT @BatchId, '{code}', o.OrderId, o.LineNumber, '{col}',
           {detail},
           '{sev}', SYSDATETIME()
      FROM #Orders AS o
     WHERE {cond};

    SET @DqIssues += @@ROWCOUNT;
""")
    return "\n".join(out)


def royalty_blocks():
    out = []
    for i, (brand, pct) in enumerate(BRAND_ROYALTIES, 1):
        out.append(f"""    -- 8.5.{i} Royalty: {brand.title().replace('_', ' ')} ({float(pct) * 100:.2f}% of net, licensed brand)
    UPDATE o
       SET o.MarginZAR = o.MarginZAR - ROUND(o.NetAmountZAR * {pct}, 2)
      FROM #Orders AS o
      JOIN #Product AS p
        ON p.ProductId = o.ProductId
     WHERE p.BrandCode = '{brand}'
       AND o.Quantity > o.ReturnedQty;
""")
    return "\n".join(out)


def payment_fee_blocks():
    out = []
    for i, (cur, pct) in enumerate(PAYMENT_FEES, 1):
        out.append(f"""    -- 8.6.{i} Payment provider fee: {cur} ({float(pct) * 100:.2f}%)
    UPDATE #Orders
       SET MarginZAR = MarginZAR - ROUND(NetAmountZAR * {pct}, 2)
     WHERE CurrencyCode = '{cur}'
       AND ChannelCode IN ('ONLINE', 'APP', 'MARKETPLACE');
""")
    return "\n".join(out)


def shipping_blocks():
    out = []
    for i, (region, amount) in enumerate(SHIPPING, 1):
        out.append(f"""    -- 8.7.{i} Shipping cost per line: region {region} (R{amount})
    UPDATE o
       SET o.CostZAR = ISNULL(o.CostZAR, 0) + {amount}
      FROM #Orders AS o
     WHERE o.RegionCode = '{region}'
       AND o.ShipDate IS NOT NULL
       AND o.ChannelCode NOT IN ('STORE');
""")
    return "\n".join(out)


def reconcile_blocks():
    out = []
    for i, m in enumerate(RECONCILE, 1):
        out.append(f"""    -- 12.2.{i} {m}: staged against loaded for the window
    INSERT INTO etl.Reconciliation (BatchId, Measure, StagedTotal, LoadedTotal, Difference, CheckedAt)
    SELECT @BatchId,
           '{m}',
           s.StagedTotal,
           f.LoadedTotal,
           ISNULL(s.StagedTotal, 0) - ISNULL(f.LoadedTotal, 0),
           SYSDATETIME()
      FROM (SELECT SUM(o.{m}) AS StagedTotal
              FROM #Orders AS o) AS s
     CROSS JOIN (SELECT SUM(fr.{m}) AS LoadedTotal
                   FROM dbo.FactRevenue AS fr
                   JOIN #Calendar AS cal
                     ON cal.DateKey = fr.DateKey) AS f;
""")
    return "\n".join(out)


def debug_snapshot(stage: str, measures: str) -> str:
    return f"""    IF @Debug = 1
    BEGIN
        SELECT N'{stage}'        AS Stage,
               o.ChannelCode,
               COUNT(*)          AS Lines,
{measures}
          FROM #Orders AS o
         GROUP BY o.ChannelCode
         ORDER BY o.ChannelCode;
    END;
"""


def log(step: str, indent: str = "    ") -> str:
    return (f"{indent}SET @Rows = @@ROWCOUNT;\n"
            f"{indent}INSERT INTO etl.LoadLog (ProcName, BatchId, Step, RowsAffected, StartedAt, Status)\n"
            f"{indent}VALUES (@ProcName, @BatchId, N'{step}', @Rows, SYSDATETIME(), 'OK');\n")


# Column lists for the debug snapshots. They live outside the f-string below because a
# backslash inside an f-string expression needs Python 3.12, and the package supports 3.9.
SNAP_PRICING = "               SUM(o.GrossAmount)    AS GrossAmount,\n               SUM(o.DiscountAmount) AS DiscountAmount,\n               SUM(o.NetAmount)      AS NetAmount"
SNAP_CONVERSION = "               SUM(o.GrossAmountZAR) AS GrossAmountZAR,\n               SUM(o.NetAmountZAR)   AS NetAmountZAR,\n               SUM(o.TaxZAR)         AS TaxZAR"
SNAP_MARGIN = "               SUM(o.CostZAR)        AS CostZAR,\n               SUM(o.MarginZAR)      AS MarginZAR"

PROC = f"""/*
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

{log("2.1 calendar")}
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

{log("2.2 fx daily")}
    -- 2.3 Active products
    SELECT p.ProductId,
           p.ProductCode,
           p.ProductName,
           p.CategoryCode,
           p.BrandCode
      INTO #Product
      FROM ref.Product AS p
     WHERE p.IsActive = 1;

{log("2.3 products")}
    -- 2.4 Customers
    SELECT c.CustomerId,
           c.CustomerCode,
           c.Segment,
           c.RegionCode,
           c.IsInternal,
           c.CreditStatus
      INTO #Customer
      FROM sales.Customer AS c;

{log("2.4 customers")}
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

{log("2.6 regions")}
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

{log("3.1 order lines")}
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

{log("3.2 duplicates")}
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

{log("3.4 internal orders")}
    -- 3.5 Marketplace orders arrive with the marketplace's own channel code; map them.
    --     The list of marketplace channel codes is configurable here (DW-1873).
    SET @MarketplaceChannels = N'''TAKEALOT'', ''AMAZON'', ''MAKRO_MP'', ''BOB_SHOP''';

    SET @sql = N'UPDATE #Orders SET ChannelCode = ''MARKETPLACE'' WHERE ChannelCode IN ('
             + @MarketplaceChannels + N');';

    IF @Debug = 1
        PRINT @sql;

    EXEC sys.sp_executesql @sql;

{log("3.5 marketplace channels")}
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

{log("4.1 promotions")}
    -- 4.2 Promotions that do not apply to this channel are dropped
    UPDATE o
       SET o.PromoCode = NULL
      FROM #Orders AS o
      JOIN sales.Promotion AS p
        ON p.PromoCode = o.PromoCode
     WHERE p.ChannelCode IS NOT NULL
       AND p.ChannelCode <> o.ChannelCode;

    -- 4.3 Channel discounts (only for lines without a promotion)
{channel_blocks()}
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
{tax_blocks()}
    UPDATE #Orders
       SET TaxAmount = ROUND(NetAmount * ISNULL(TaxRate, 0), 2);

{log("4 pricing")}
{debug_snapshot("after pricing", SNAP_PRICING)}
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

{log("5.1 conversion")}
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

{log("5.2 default rates")}
{debug_snapshot("after conversion", SNAP_CONVERSION)}
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

{log("6 returns", "        ")}
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

{log("7.1 adjustments", "        ")}
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

{log("7.2 price corrections", "        ")}
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

{log("7.3 goodwill credits", "        ")}
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

{log("8.1 cost")}
    -- 8.2 Returned units carry no cost
    UPDATE #Orders
       SET CostZAR = ROUND(CostZAR * (Quantity - ReturnedQty) / NULLIF(Quantity, 0), 2)
     WHERE ReturnedQty > 0;

    -- 8.3 Handling uplift per product category
{uplift_blocks()}
    -- 8.4 Shipping cost per region (lines that were shipped)
{shipping_blocks()}
    -- 8.4 Margin
    UPDATE #Orders
       SET MarginZAR = NetAmountZAR - ReturnedZAR - ISNULL(CostZAR, 0);

    -- 8.5 Brand royalties reduce the margin
{royalty_blocks()}
    -- 8.6 Payment provider fees reduce the margin
{payment_fee_blocks()}
{log("8 margin")}
{debug_snapshot("after margin", SNAP_MARGIN)}
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
{commission_blocks()}
    -- 9.3 No commission on staff purchases or fully returned lines
    UPDATE #Orders
       SET CommissionZAR = 0
     WHERE Segment = 'STAFF'
        OR ReturnedQty >= Quantity;

{log("9 commission")}
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

{log("10 region summary")}
    /* ==========================================================================================
       11. Data-quality checks
       ========================================================================================== */
{dq_blocks()}
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
{reconcile_blocks()}
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
"""

SCHEMA = """-- Tables used by etl.usp_LoadFactRevenue (synthetic test material for sql-doc-gen).
CREATE TABLE sales.OrderHeader (
    OrderId      int          NOT NULL PRIMARY KEY,
    OrderNumber  varchar(20)  NOT NULL,
    CustomerId   int          NOT NULL,
    OrderDate    date         NOT NULL,
    ShipDate     date         NULL,
    ChannelCode  varchar(20)  NOT NULL,
    RegionCode   char(2)      NULL,
    CurrencyCode char(3)      NOT NULL,
    OrderStatus  varchar(20)  NOT NULL,
    SalesRepId   int          NULL,
    PromoCode    varchar(20)  NULL,
    ModifiedAt   datetime2(3) NOT NULL
);
GO
CREATE TABLE sales.OrderLine (
    OrderId      int           NOT NULL,
    LineNumber   int           NOT NULL,
    ProductId    int           NOT NULL,
    Quantity     int           NOT NULL,
    UnitPrice    decimal(18,4) NOT NULL,
    LineDiscount decimal(18,4) NULL,
    TaxRate      decimal(9,4)  NULL,
    CONSTRAINT PK_OrderLine PRIMARY KEY (OrderId, LineNumber)
);
GO
CREATE TABLE sales.ReturnLine (
    ReturnId     int           NOT NULL,
    OrderId      int           NOT NULL,
    LineNumber   int           NOT NULL,
    ReturnDate   date          NOT NULL,
    ReturnQty    int           NOT NULL,
    RefundAmount decimal(18,4) NOT NULL,
    Reason       varchar(50)   NULL,
    CONSTRAINT PK_ReturnLine PRIMARY KEY (ReturnId, OrderId, LineNumber)
);
GO
CREATE TABLE sales.ManualAdjustment (
    AdjustmentId   int           NOT NULL PRIMARY KEY,
    OrderId        int           NOT NULL,
    LineNumber     int           NOT NULL,
    AdjustmentType varchar(20)   NOT NULL,
    Amount         decimal(18,4) NOT NULL,
    CurrencyCode   char(3)       NOT NULL,
    EffectiveDate  date          NOT NULL,
    ApprovedBy     varchar(50)   NULL,
    IsApproved     bit           NOT NULL
);
GO
CREATE TABLE sales.Customer (
    CustomerId   int           NOT NULL PRIMARY KEY,
    CustomerCode varchar(20)   NOT NULL,
    CustomerName nvarchar(200) NOT NULL,
    Segment      varchar(20)   NOT NULL,
    RegionCode   char(2)       NULL,
    IsInternal   bit           NOT NULL,
    CreditStatus varchar(20)   NULL
);
GO
CREATE TABLE sales.Promotion (
    PromoCode    varchar(20)  NOT NULL PRIMARY KEY,
    DiscountPct  decimal(9,4) NOT NULL,
    ValidFrom    date         NOT NULL,
    ValidTo      date         NOT NULL,
    ChannelCode  varchar(20)  NULL
);
GO
CREATE TABLE ref.FxRates (
    CurrencyCode char(3)       NOT NULL,
    RateDate     date          NOT NULL,
    RateToZAR    decimal(18,8) NOT NULL,
    CONSTRAINT PK_FxRates PRIMARY KEY (CurrencyCode, RateDate)
);
GO
CREATE TABLE ref.Product (
    ProductId    int           NOT NULL PRIMARY KEY,
    ProductCode  varchar(30)   NOT NULL,
    ProductName  nvarchar(200) NOT NULL,
    CategoryCode varchar(20)   NOT NULL,
    BrandCode    varchar(20)   NULL,
    IsActive     bit           NOT NULL
);
GO
CREATE TABLE ref.ProductCost (
    ProductId     int           NOT NULL,
    EffectiveFrom date          NOT NULL,
    UnitCostZAR   decimal(18,4) NOT NULL,
    CONSTRAINT PK_ProductCost PRIMARY KEY (ProductId, EffectiveFrom)
);
GO
CREATE TABLE ref.Channel (
    ChannelCode   varchar(20)  NOT NULL PRIMARY KEY,
    ChannelName   nvarchar(100) NOT NULL,
    ChannelGroup  varchar(20)  NOT NULL,
    CommissionPct decimal(9,4) NOT NULL
);
GO
CREATE TABLE ref.Region (
    RegionCode char(2)       NOT NULL PRIMARY KEY,
    RegionName nvarchar(100) NOT NULL,
    Territory  varchar(20)   NULL,
    IsEU       bit           NOT NULL
);
GO
CREATE TABLE ref.SalesRep (
    SalesRepId int           NOT NULL PRIMARY KEY,
    RepName    nvarchar(100) NOT NULL,
    TeamCode   varchar(20)   NOT NULL,
    ManagerId  int           NULL
);
GO
CREATE TABLE ref.RegionTarget (
    RegionCode  char(2)       NOT NULL,
    PeriodMonth date          NOT NULL,
    TargetZAR   decimal(18,2) NOT NULL,
    CONSTRAINT PK_RegionTarget PRIMARY KEY (RegionCode, PeriodMonth)
);
GO
CREATE TABLE dbo.DimDate (
    DateKey      int  NOT NULL PRIMARY KEY,
    CalendarDate date NOT NULL,
    FiscalYear   int  NOT NULL,
    FiscalPeriod int  NOT NULL,
    IsWeekend    bit  NOT NULL,
    IsHoliday    bit  NOT NULL
);
GO
CREATE TABLE dbo.FactRevenue (
    OrderId        int           NOT NULL,
    LineNumber     int           NOT NULL,
    DateKey        int           NULL,
    CustomerId     int           NOT NULL,
    ProductId      int           NOT NULL,
    ChannelCode    varchar(20)   NOT NULL,
    RegionCode     char(2)       NULL,
    Quantity       int           NOT NULL,
    GrossAmountZAR decimal(18,2) NULL,
    DiscountZAR    decimal(18,2) NULL,
    NetAmountZAR   decimal(18,2) NULL,
    TaxZAR         decimal(18,2) NULL,
    CostZAR        decimal(18,2) NULL,
    MarginZAR      decimal(18,2) NULL,
    ReturnedZAR    decimal(18,2) NULL,
    CommissionZAR  decimal(18,2) NULL,
    PromoCode      varchar(20)   NULL,
    IsAdjusted     bit           NOT NULL,
    RowHash        varbinary(32) NULL,
    LoadBatchId    int           NULL,
    LoadedAt       datetime2(3)  NULL,
    CONSTRAINT PK_FactRevenue PRIMARY KEY (OrderId, LineNumber)
);
GO
CREATE TABLE etl.LoadBatch (
    BatchId    int IDENTITY(1,1) NOT NULL PRIMARY KEY,
    ProcName   nvarchar(256) NOT NULL,
    LoadDate   date          NOT NULL,
    Mode       varchar(20)   NOT NULL,
    StartedAt  datetime2(3)  NOT NULL,
    FinishedAt datetime2(3)  NULL,
    Status     varchar(20)   NOT NULL,
    RowsLoaded int           NULL,
    DqIssues   int           NULL
);
GO
CREATE TABLE etl.LoadLog (
    LogId        int IDENTITY(1,1) NOT NULL PRIMARY KEY,
    ProcName     nvarchar(256)  NOT NULL,
    BatchId      int            NULL,
    Step         nvarchar(200)  NOT NULL,
    RowsAffected int            NULL,
    StartedAt    datetime2(3)   NOT NULL,
    Status       varchar(20)    NOT NULL,
    Message      nvarchar(4000) NULL
);
GO
CREATE TABLE etl.DataQualityIssue (
    IssueId    int IDENTITY(1,1) NOT NULL PRIMARY KEY,
    BatchId    int            NOT NULL,
    RuleCode   varchar(50)    NOT NULL,
    OrderId    int            NULL,
    LineNumber int            NULL,
    ColumnName varchar(100)   NULL,
    Detail     nvarchar(4000) NULL,
    Severity   varchar(10)    NOT NULL,
    DetectedAt datetime2(3)   NOT NULL
);
GO
CREATE TABLE etl.RegionSummary (
    BatchId      int           NOT NULL,
    RegionCode   char(2)       NOT NULL,
    PeriodMonth  date          NOT NULL,
    NetAmountZAR decimal(18,2) NULL,
    TargetZAR    decimal(18,2) NULL,
    Attainment   decimal(9,4)  NULL
);
GO
CREATE TABLE etl.FactRevenueAudit (
    AuditId         int IDENTITY(1,1) NOT NULL PRIMARY KEY,
    BatchId         int           NOT NULL,
    Action          nvarchar(10)  NOT NULL,
    OrderId         int           NOT NULL,
    LineNumber      int           NOT NULL,
    OldNetAmountZAR decimal(18,2) NULL,
    NewNetAmountZAR decimal(18,2) NULL,
    ChangedAt       datetime2(3)  NOT NULL
);
GO
CREATE TABLE etl.Reconciliation (
    BatchId     int           NOT NULL,
    Measure     varchar(50)   NOT NULL,
    StagedTotal decimal(19,2) NULL,
    LoadedTotal decimal(19,2) NULL,
    Difference  decimal(19,2) NULL,
    CheckedAt   datetime2(3)  NOT NULL
);
GO
CREATE TABLE etl.ErrorLog (
    ErrorId       int IDENTITY(1,1) NOT NULL PRIMARY KEY,
    ProcName      nvarchar(256)  NOT NULL,
    BatchId       int            NULL,
    ErrorNumber   int            NULL,
    ErrorSeverity int            NULL,
    ErrorState    int            NULL,
    ErrorLine     int            NULL,
    ErrorMessage  nvarchar(4000) NULL,
    LoggedAt      datetime2(3)   NOT NULL
);
GO
"""


def main():
    (HERE / "usp_LoadFactRevenue.sql").write_text(PROC.replace("\r\n", "\n"), encoding="utf-8")
    (HERE / "schema.sql").write_text(SCHEMA, encoding="utf-8")
    print(f"usp_LoadFactRevenue.sql: {PROC.count(chr(10)) + 1} lines")


if __name__ == "__main__":
    main()
