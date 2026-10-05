"""Decoding, offsets, comments and secret masking (no parser needed)."""
from __future__ import annotations

import codecs
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401  (puts the repository on sys.path)

from sqldocgen.textutil import (LineIndex, Utf16Map, decode_sql_bytes, extract_comments,  # noqa: E402
                                mask_secrets, scan_tokens, scrub_text)


class Decoding(unittest.TestCase):
    def test_utf8_bom(self):
        self.assertEqual(decode_sql_bytes(codecs.BOM_UTF8 + "SELECT N'é'".encode("utf-8")), ("SELECT N'é'", "utf-8-sig"))

    def test_utf16_with_bom(self):
        text = "SELECT N'Zoë' -- ✓\r\n"
        self.assertEqual(decode_sql_bytes(codecs.BOM_UTF16_LE + text.encode("utf-16-le")), (text, "utf-16"))
        self.assertEqual(decode_sql_bytes(codecs.BOM_UTF16_BE + text.encode("utf-16-be"))[0], text)

    def test_utf16_without_bom(self):
        self.assertEqual(decode_sql_bytes("SELECT 1".encode("utf-16-le")), ("SELECT 1", "utf-16-le"))

    def test_plain_utf8_and_windows_1252(self):
        self.assertEqual(decode_sql_bytes(b"SELECT 1\r\nGO\r\n"), ("SELECT 1\r\nGO\r\n", "utf-8"))
        self.assertEqual(decode_sql_bytes(b"SELECT '\xe9t\xe9'"), ("SELECT 'été'", "cp1252"))


class Offsets(unittest.TestCase):
    def test_line_index(self):
        li = LineIndex("a\nbb\r\nccc")
        self.assertEqual([li.line(i) for i in (0, 1, 2, 5, 6, 8)], [1, 1, 2, 2, 3, 3])
        self.assertEqual(li.lines((2, 9)), (2, 3))
        self.assertEqual(li.lines((2, 4)), (2, 2))
        self.assertIsNone(li.lines(None))

    def test_utf16_map_matches_real_encoding(self):
        text = "SELECT N'😀' AS a, N'𝔘' AS b -- 🚀 done\nSELECT 'é'"
        m = Utf16Map(text)
        self.assertFalse(m.identity)
        for cp in range(len(text) + 1):
            u16 = len(text[:cp].encode("utf-16-le")) // 2
            self.assertEqual(m.to_u16(cp), u16, cp)
            self.assertEqual(m.to_cp(u16), cp, cp)

    def test_utf16_map_identity_for_bmp_text(self):
        m = Utf16Map("SELECT 'é ✓ ж'")
        self.assertTrue(m.identity)
        self.assertEqual(m.to_cp(7), 7)
        self.assertEqual(m.to_u16(7), 7)


class Lexing(unittest.TestCase):
    def test_scan_tokens(self):
        text = "SELECT 'it''s', N'x', [a]]b], \"q\" /* a /* nested */ b */ -- tail\nSELECT 1"
        kinds = [(k, text[s:e]) for k, s, e in scan_tokens(text)]
        self.assertEqual(kinds, [("string", "'it''s'"), ("string", "N'x'"), ("quoted", "[a]]b]"), ("quoted", '"q"'),
                                 ("comment", "/* a /* nested */ b */"), ("comment", "-- tail")])

    def test_strings_hide_comment_markers(self):
        text = "SELECT '-- not a comment', '/* nor this */' -- real"
        self.assertEqual([text[s:e] for k, s, e in scan_tokens(text) if k == "comment"], ["-- real"])

    def test_extract_comments(self):
        text = "-- Load the fact\nSELECT 1\n/*\n * Second line\n * Third line\n */"
        bodies = [b for _, _, b in extract_comments(text)]
        self.assertEqual(bodies, ["Load the fact", "Second line\nThird line"])


class Secrets(unittest.TestCase):
    def masked(self, text, expect_count=None):
        out, n = mask_secrets(text)
        self.assertEqual(len(out), len(text), "masking keeps every offset")
        if expect_count is not None:
            self.assertEqual(n, expect_count, out)
        return out

    def test_openrowset_connection_string(self):
        out = self.masked("SELECT * FROM OPENROWSET('SQLNCLI', 'Server=etl01;UID=loader;PWD=Tr0ub4dor&3;', "
                          "'SELECT 1 AS x') AS r;", 1)
        self.assertNotIn("Tr0ub4dor", out)
        self.assertIn("UID=loader", out, "only the secret value is masked")
        self.assertIn("PWD=***********;", out)

    def test_opendatasource_and_storage_keys(self):
        out = self.masked("SELECT * FROM OPENDATASOURCE('SQLNCLI', 'Data Source=x;User ID=u;Password=hunter22')"
                          ".db.dbo.t; SELECT 'DefaultEndpointsProtocol=https;AccountName=a;AccountKey=QUJDREVGRw==;'")
        self.assertNotIn("hunter22", out)
        self.assertNotIn("QUJDREVGRw", out)

    def test_create_login_and_credentials(self):
        out = self.masked("CREATE LOGIN etl WITH PASSWORD = N'P@ssw0rd!' MUST_CHANGE; "
                          "CREATE DATABASE SCOPED CREDENTIAL c WITH IDENTITY = 'svc', SECRET = 'sv=2022&sig=abc';"
                          "ALTER LOGIN etl WITH PASSWORD = 'n3w' OLD_PASSWORD = 'old!';", 4)
        for leaked in ("P@ssw0rd!", "sv=2022", "n3w", "old!"):
            self.assertNotIn(leaked, out)
        self.assertIn("IDENTITY = 'svc'", out)

    def test_hashed_password_keeps_valid_binary(self):
        out = self.masked("CREATE LOGIN x WITH PASSWORD = 0x0200A1B2C3 HASHED, SID = 0x1234;", 1)
        self.assertIn("PASSWORD = 0x0000000000 HASHED", out)
        self.assertIn("SID = 0x1234", out)

    def test_linked_server_login_positional_and_named(self):
        out = self.masked("EXEC sp_addlinkedsrvlogin 'REMOTE', 'false', NULL, 'remoteuser', 'pw-123';", 1)
        self.assertNotIn("pw-123", out)
        self.assertIn("'remoteuser'", out)
        out = self.masked("EXEC master.dbo.sp_addlinkedsrvlogin @rmtsrvname = N'REMOTE', @useself = N'False', "
                          "@locallogin = NULL, @rmtuser = N'u', @rmtpassword = N'hunter2';", 1)
        self.assertNotIn("hunter2", out)

    def test_passwords_in_dynamic_sql(self):
        out = self.masked("SET @sql = N'CREATE LOGIN bob WITH PASSWORD = ''Xy!9''';", 1)
        self.assertIn("PASSWORD = ''****''';", out)
        out = self.masked("SET @sql = N'EXEC sp_addlinkedsrvlogin ''R'', ''false'', NULL, ''u'', ''pw9''';", 1)
        self.assertNotIn("pw9", out)
        out = self.masked("SET @sql = N'EXEC sp_addlinkedsrvlogin @rmtsrvname = ''R'', @rmtpassword = ''a''''b'';';", 1)
        self.assertIn("@rmtpassword = ''******'';", out, "an escaped quote inside the secret is masked too")
        out = self.masked("SET @o = N'SET @i = N''CREATE LOGIN z WITH PASSWORD = ''''deep''''''''; EXEC (@i);';", 1)
        self.assertNotIn("deep", out)
        self.assertEqual(out.count("'"), "SET @o = N'SET @i = N''CREATE LOGIN z WITH PASSWORD = ''''deep''''''''; EXEC (@i);';".count("'"),
                         "the quoting of nested dynamic SQL is untouched")

    def test_commented_out_secrets(self):
        out = self.masked("-- CREATE LOGIN etl WITH PASSWORD = 'Winter2024!'\nSELECT 1", 1)
        self.assertNotIn("Winter2024", out)
        out = self.masked("/* Server=db1;User Id=etl;Password=S3cret; */ SELECT 1", 1)
        self.assertNotIn("S3cret", out)

    def test_ordinary_code_untouched(self):
        text = ("SELECT 'it''s fine', N'no secrets here', p.PasswordHash, k.KeyName -- primary key\n"
                "FROM dbo.Users AS p WHERE p.LastPasswordChange < @d; DECLARE @password_rule int = 3;")
        self.assertEqual(self.masked(text, 0), text)

    def test_scrub_text(self):
        out, n = scrub_text("conn: Server=x;Password=abc;  auth: Bearer abcdefghijklmnop1234")
        self.assertNotIn("abc;", out)
        self.assertNotIn("abcdefghijklmnop1234", out)
        self.assertGreaterEqual(n, 2)
        same, n = scrub_text("Password=****")
        self.assertEqual((same, n), ("Password=****", 0), "already-masked values are left alone")


if __name__ == "__main__":
    unittest.main()
