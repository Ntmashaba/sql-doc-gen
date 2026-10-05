"""Table definitions from a DACPAC (model.xml) and from an exported column list (CSV)."""
from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as h  # noqa: E402
from helpers import direct_sources, output  # noqa: E402

from sqldocgen.catalog import Catalog, load_columns_csv, load_dacpac  # noqa: E402

NS = "http://schemas.microsoft.com/sqlserver/dac/Serialization/2012/02"


def _type(name, **props):
    p = "".join(f'<Property Name="{k}" Value="{v}" />' for k, v in props.items())
    return (f'<Element Type="SqlTypeSpecifier">{p}<Relationship Name="Type"><Entry>'
            f'<References ExternalSource="BuiltIns" Name="[{name}]" /></Entry></Relationship></Element>')


def _column(table, name, type_xml, nullable=True, identity=False):
    props = f'<Property Name="IsNullable" Value="{nullable}" />'
    if identity:
        props += '<Property Name="IsIdentity" Value="True" />'
    return (f'<Entry><Element Type="SqlSimpleColumn" Name="{table}.[{name}]">{props}'
            f'<Relationship Name="TypeSpecifier"><Entry>{type_xml}</Entry></Relationship></Element></Entry>')


MODEL = f"""<?xml version="1.0" encoding="utf-8"?>
<DataSchemaModel FileFormatVersion="1.2" SchemaVersion="2.9"
    DspName="Microsoft.Data.Tools.Schema.Sql.Sql160DatabaseSchemaProvider" xmlns="{NS}">
  <Model>
    <!-- real model.xml files sort elements by type: the key comes before its table -->
    <Element Type="SqlPrimaryKeyConstraint" Name="[dbo].[PK_Orders]">
      <Relationship Name="ColumnSpecifications"><Entry><Element Type="SqlIndexedColumnSpecification">
        <Relationship Name="Column"><Entry><References Name="[dbo].[Orders].[OrderId]" /></Entry></Relationship>
      </Element></Entry></Relationship>
      <Relationship Name="DefiningTable"><Entry><References Name="[dbo].[Orders]" /></Entry></Relationship>
    </Element>
    <Element Type="SqlTable" Name="[dbo].[Orders]">
      <Relationship Name="Columns">
        {_column("[dbo].[Orders]", "OrderId", _type("int"), nullable=False, identity=True)}
        {_column("[dbo].[Orders]", "Amount", _type("decimal", Precision=18, Scale=2))}
        {_column("[dbo].[Orders]", "Note", _type("nvarchar", IsMax="True"))}
        {_column("[dbo].[Orders]", "Code", _type("varchar", Length=10))}
      </Relationship>
    </Element>
    <Element Type="SqlTable" Name="[dbo].[Archive]">
      <Relationship Name="Columns">
        {_column("[dbo].[Archive]", "OrderId", _type("int"))}
        {_column("[dbo].[Archive]", "Amount", _type("decimal", Precision=18, Scale=2))}
      </Relationship>
    </Element>
    <Element Type="SqlView" Name="[dbo].[vBigOrders]">
      <Property Name="QueryScript"><Value><![CDATA[SELECT o.OrderId, o.Amount FROM dbo.Orders AS o WHERE o.Amount > 1000]]></Value></Property>
    </Element>
    <Element Type="SqlProcedure" Name="[dbo].[usp_ArchiveBig]">
      <Property Name="BodyScript"><Value><![CDATA[BEGIN
    SET NOCOUNT ON;
    INSERT INTO dbo.Archive (OrderId, Amount)
    SELECT v.OrderId, v.Amount * @Factor FROM dbo.vBigOrders AS v;
END]]></Value></Property>
      <Relationship Name="Parameters"><Entry>
        <Element Type="SqlSubroutineParameter" Name="[dbo].[usp_ArchiveBig].[@Factor]">
          <Property Name="DefaultExpressionScript"><Value><![CDATA[1.0]]></Value></Property>
          <Relationship Name="Type"><Entry>{_type("decimal", Precision=9, Scale=4)}</Entry></Relationship>
        </Element>
      </Entry></Relationship>
    </Element>
  </Model>
</DataSchemaModel>
"""
METADATA = f"""<?xml version="1.0" encoding="utf-8"?>
<DacType xmlns="{NS}"><Name>SalesDb</Name><Version>1.0.0.0</Version></DacType>
"""
COPY_PROC = """CREATE PROCEDURE dbo.usp_CopyOrders AS
BEGIN
    SELECT * INTO #o FROM dbo.Orders;
    INSERT INTO dbo.Archive (OrderId, Amount) SELECT OrderId, Amount FROM #o;
    SELECT * FROM #o;
END
"""
CSV = """TABLE_CATALOG,TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,ORDINAL_POSITION,DATA_TYPE,CHARACTER_MAXIMUM_LENGTH,NUMERIC_PRECISION,NUMERIC_SCALE,IS_NULLABLE
SalesDb,dbo,Orders,Amount,2,decimal,NULL,18,2,YES
SalesDb,dbo,Orders,OrderId,1,int,NULL,10,0,NO
SalesDb,dbo,Orders,Note,3,nvarchar,-1,NULL,NULL,YES
SalesDb,dbo,Archive,OrderId,1,int,NULL,10,0,YES
SalesDb,dbo,Archive,Amount,2,decimal,NULL,18,2,YES
"""


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.dacpac = d / "SalesDb.dacpac"
        with zipfile.ZipFile(cls.dacpac, "w") as z:
            z.writestr("model.xml", MODEL)
            z.writestr("DacMetadata.xml", METADATA)
        cls.csv = d / "columns.csv"
        cls.csv.write_text(CSV, encoding="utf-8")
        cls.proc = d / "usp_CopyOrders.sql"
        cls.proc.write_text(COPY_PROC, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class DacpacLoader(Fixture):
    def test_tables_columns_types_keys(self):
        cat = Catalog()
        scripts = load_dacpac(self.dacpac, cat)
        self.assertEqual(cat.database, "SalesDb")
        orders = cat.find("", "", "dbo", "orders")
        self.assertIsNotNone(orders, "names match without regard to case")
        self.assertEqual([(c.name, c.type) for c in orders.columns],
                         [("OrderId", "int"), ("Amount", "decimal(18,2)"), ("Note", "nvarchar(max)"), ("Code", "varchar(10)")])
        self.assertEqual(orders.columns[0].nullable, False)
        self.assertTrue(orders.columns[0].identity)
        self.assertEqual(orders.keys, [["OrderId"]])
        self.assertIn("DACPAC SalesDb.dacpac", cat.sources)
        ids = [i for i, _, _ in scripts]
        self.assertIn("dacpac:[dbo].[vBigOrders]", ids)
        self.assertIn("dacpac:[dbo].[usp_ArchiveBig]", ids)
        proc = dict((i, t) for i, t, _ in scripts)["dacpac:[dbo].[usp_ArchiveBig]"]
        self.assertIn("@Factor decimal(9,4) = 1.0", proc)


class CsvLoader(Fixture):
    def test_information_schema_export(self):
        cat = Catalog()
        self.assertEqual(load_columns_csv(self.csv, cat), 2)
        orders = cat.find("", "", "dbo", "Orders")
        self.assertEqual([(c.name, c.type, c.nullable) for c in orders.columns],
                         [("OrderId", "int", False), ("Amount", "decimal(18,2)", True), ("Note", "nvarchar(max)", True)],
                         "ordered by ORDINAL_POSITION, not file order")

    def test_needs_table_and_column(self):
        bad = Path(self.tmp.name) / "bad.csv"
        bad.write_text("a,b\n1,2\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_columns_csv(bad, Catalog())


@h.requires_parser
class WithDefinitions(Fixture):
    def check_star_expanded(self, schema):
        p = h.document([self.proc], schema=[schema])["dbo.usp_CopyOrders"]
        self.assertEqual(p["mode"], "schema")
        res = {o["col"] for o in p["outputs"] if h.rel_name(p, o["rel"]) == "Result set 1"}
        self.assertTrue({"OrderId", "Amount", "Note"} <= res, res)
        self.assertEqual(direct_sources(p, "dbo.Archive", "Amount"), {"dbo.Orders.Amount"})
        self.assertEqual(output(p, "dbo.Archive", "Amount")["status"], "resolved")
        return p

    def test_dacpac_as_schema(self):
        self.check_star_expanded(self.dacpac)

    def test_csv_as_schema(self):
        self.check_star_expanded(self.csv)

    def test_dacpac_as_input_documents_its_procedures(self):
        docs = h.document([self.dacpac])
        self.assertIn("dbo.usp_ArchiveBig", docs)
        p = docs["dbo.usp_ArchiveBig"]
        self.assertEqual(p["mode"], "project")
        self.assertEqual(direct_sources(p, "dbo.Archive", "Amount"), {"dbo.Orders.Amount", "@Factor.value"},
                         "the view is expanded to the table under it")


if __name__ == "__main__":
    unittest.main()
