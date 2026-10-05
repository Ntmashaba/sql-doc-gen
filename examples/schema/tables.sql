-- Table definitions used by the scenario procedures ("with schema" mode).
CREATE TABLE Sales.Orders (
    OrderId      int           NOT NULL PRIMARY KEY,
    CustomerId   int           NOT NULL,
    OrderDate    date          NOT NULL,
    ShipDate     date          NULL,
    Amount       decimal(18,2) NOT NULL,
    Discount     decimal(18,2) NULL,
    CurrencyCode char(3)       NOT NULL,
    Status       varchar(20)   NOT NULL
);
GO
CREATE TABLE Sales.OrderLines (
    OrderId   int           NOT NULL,
    LineNo    int           NOT NULL,
    ProductId int           NOT NULL,
    Quantity  int           NOT NULL,
    UnitPrice decimal(18,2) NOT NULL,
    CONSTRAINT PK_OrderLines PRIMARY KEY (OrderId, LineNo)
);
GO
CREATE TABLE Sales.Customers (
    CustomerId   int          NOT NULL PRIMARY KEY,
    CustomerName nvarchar(200) NOT NULL,
    Region       varchar(20)  NULL,
    Segment      varchar(20)  NULL,
    CreditLimit  decimal(18,2) NULL
);
GO
CREATE TABLE Ref.FxRates (
    CurrencyCode char(3)       NOT NULL,
    RateDate     date          NOT NULL,
    Rate         decimal(18,6) NOT NULL,
    CONSTRAINT PK_FxRates PRIMARY KEY (CurrencyCode, RateDate)
);
GO
CREATE TABLE Ref.Products (
    ProductId   int          NOT NULL PRIMARY KEY,
    ProductCode varchar(30)  NOT NULL,
    Category    varchar(50)  NULL,
    ListPrice   decimal(18,2) NULL
);
GO
CREATE TABLE Ref.Employees (
    EmployeeId int          NOT NULL PRIMARY KEY,
    ManagerId  int          NULL,
    FullName   nvarchar(200) NOT NULL
);
GO
CREATE TABLE dbo.DailyRevenue (
    OrderId   int           NOT NULL,
    Region    varchar(20)   NULL,
    AmountZar decimal(18,2) NULL,
    LoadDate  date          NOT NULL
);
GO
CREATE TABLE dbo.Prices (
    ProductId    int           NOT NULL PRIMARY KEY,
    CurrencyCode char(3)       NOT NULL,
    Price        decimal(18,2) NOT NULL,
    PriceZar     decimal(18,2) NULL
);
GO
CREATE TABLE dbo.CustomerScore (
    CustomerId int          NOT NULL PRIMARY KEY,
    Score      int          NULL,
    Band       varchar(10)  NULL,
    UpdatedAt  datetime2    NULL
);
GO
CREATE TABLE dbo.FactSales (
    OrderId     int           NOT NULL PRIMARY KEY,
    CustomerId  int           NOT NULL,
    NetAmount   decimal(18,2) NULL,
    RowHash     varbinary(32) NULL,
    IsDeleted   bit           NOT NULL DEFAULT 0,
    LoadedAt    datetime2     NULL
);
GO
CREATE TABLE dbo.FactSalesAudit (
    Action   nvarchar(10) NULL,
    OrderId  int          NULL,
    OldNet   decimal(18,2) NULL,
    NewNet   decimal(18,2) NULL
);
GO
CREATE TABLE dbo.OrgChart (
    EmployeeId int NOT NULL,
    ManagerId  int NULL,
    Level      int NOT NULL,
    Path       nvarchar(4000) NULL
);
GO
CREATE TABLE dbo.ProductSummary (
    Category    varchar(50)   NULL,
    Products    int           NULL,
    Revenue     decimal(18,2) NULL,
    TopProduct  varchar(30)   NULL
);
GO
CREATE TABLE dbo.LoadLog (
    LogId      int IDENTITY(1,1) NOT NULL PRIMARY KEY,
    Step       varchar(100) NOT NULL,
    RowsAffected int        NULL,
    LoggedAt   datetime2    NOT NULL DEFAULT SYSDATETIME()
);
GO
CREATE TABLE dbo.ErrorLog (
    ErrorId      int IDENTITY(1,1) NOT NULL PRIMARY KEY,
    ErrorNumber  int NULL,
    ErrorMessage nvarchar(4000) NULL,
    LoggedAt     datetime2 NOT NULL DEFAULT SYSDATETIME()
);
GO
CREATE TABLE dbo.CustomerSnapshot (
    CustomerId   int           NOT NULL,
    CustomerName nvarchar(200) NULL,
    Region       varchar(20)   NULL,
    Segment      varchar(20)   NULL,
    CreditLimit  decimal(18,2) NULL
);
GO
CREATE TABLE dbo.Commission (
    EmployeeId  int           NOT NULL,
    PeriodEnd   date          NOT NULL,
    Commission  decimal(18,2) NULL
);
GO
