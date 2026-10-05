-- Tables used by etl.usp_LoadFactRevenue (synthetic test material for sql-doc-gen).
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
