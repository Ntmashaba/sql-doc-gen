// sqldocgen-parser
//
// Parses T-SQL with Microsoft ScriptDom and writes the syntax tree as JSON, so that the
// Python side of sql-doc-gen can analyse it. The helper is deliberately generic: it does no
// analysis of its own. Every ScriptDom node becomes a JSON object
//
//     {"$": "<ScriptDom class name>", "@": [offset, length, line, column], "<Property>": ...}
//
// Offsets and lengths are UTF-16 code units into the text that was parsed. Child nodes are
// objects, lists of nodes are arrays (empty lists and null properties are left out), enums
// are their names, and false booleans are left out.
//
// Usage
//   sqldocgen-parser [--parser auto|TSql170|...] [--no-quoted-identifier] < request.json
//       request:  {"parser": "auto", "quotedIdentifier": true, "items": [{"id": "a", "text": "..."}]}
//   sqldocgen-parser FILE.sql [--parser ...]          parse one file (debugging)
//   sqldocgen-parser --version | --list-parsers
//
// Response: {"helper": "sqldocgen-parser", "helperVersion": ..., "scriptDom": ..., "parser": ...,
//            "results": [{"id": ..., "errors": [{"number", "offset", "line", "column", "message"}], "tree": {...}}]}

using System.Collections;
using System.Diagnostics;
using System.Reflection;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using Microsoft.SqlServer.TransactSql.ScriptDom;

static class Program
{
    const string HelperVersion = "1.0.0";

    static int Main(string[] args)
    {
        int rc = 0;
        // Very long expressions (string concatenations of hundreds of parts) nest deeply;
        // walk them on a thread with a large stack.
        var worker = new Thread(() => rc = Run(args), 512 * 1024 * 1024);
        worker.Start();
        worker.Join();
        return rc;
    }

    static int Run(string[] args)
    {
        try
        {
            string parserName = "auto";
            bool quoted = true;
            string file = null;
            for (int i = 0; i < args.Length; i++)
            {
                switch (args[i])
                {
                    case "--version":
                        Console.WriteLine($"sqldocgen-parser {HelperVersion}; ScriptDom {ScriptDomVersion()}");
                        return 0;
                    case "--list-parsers":
                        foreach (var p in ParserTypes().OrderBy(p => p.Key)) Console.WriteLine(p.Value.Name);
                        return 0;
                    case "--parser":
                        parserName = args[++i];
                        break;
                    case "--no-quoted-identifier":
                        quoted = false;
                        break;
                    default:
                        file = args[i];
                        break;
                }
            }

            var items = new List<(string id, string text)>();
            if (file != null && file != "-")
            {
                items.Add((file, File.ReadAllText(file)));
            }
            else
            {
                string input;
                using (var reader = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false)))
                    input = reader.ReadToEnd();
                using var doc = JsonDocument.Parse(input, new JsonDocumentOptions { MaxDepth = 64 });
                var root = doc.RootElement;
                if (root.TryGetProperty("parser", out var pn) && pn.ValueKind == JsonValueKind.String) parserName = pn.GetString();
                if (root.TryGetProperty("quotedIdentifier", out var qi) && (qi.ValueKind == JsonValueKind.False)) quoted = false;
                foreach (var it in root.GetProperty("items").EnumerateArray())
                    items.Add((it.GetProperty("id").GetString(), it.GetProperty("text").GetString() ?? ""));
            }

            var parserType = PickParser(parserName);
            var stdout = new BufferedStream(Console.OpenStandardOutput(), 1 << 16);
            var writer = new Utf8JsonWriter(stdout, new JsonWriterOptions
            {
                Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
                Indented = false,
                SkipValidation = true,
            });
            writer.WriteStartObject();
            writer.WriteString("helper", "sqldocgen-parser");
            writer.WriteString("helperVersion", HelperVersion);
            writer.WriteString("scriptDom", ScriptDomVersion());
            writer.WriteString("parser", parserType.Name);
            writer.WriteBoolean("quotedIdentifier", quoted);
            writer.WritePropertyName("results");
            writer.WriteStartArray();
            foreach (var (id, text) in items)
            {
                var parser = (TSqlParser)Activator.CreateInstance(parserType, new object[] { quoted });
                IList<ParseError> errors;
                TSqlFragment tree;
                using (var sr = new StringReader(text))
                    tree = parser.Parse(sr, out errors);
                writer.WriteStartObject();
                writer.WriteString("id", id);
                writer.WritePropertyName("errors");
                writer.WriteStartArray();
                foreach (var e in errors ?? new List<ParseError>())
                {
                    writer.WriteStartObject();
                    writer.WriteNumber("number", e.Number);
                    writer.WriteNumber("offset", e.Offset);
                    writer.WriteNumber("line", e.Line);
                    writer.WriteNumber("column", e.Column);
                    writer.WriteString("message", e.Message);
                    writer.WriteEndObject();
                }
                writer.WriteEndArray();
                writer.WritePropertyName("tree");
                if (tree == null) writer.WriteNullValue(); else WriteNode(writer, tree);
                writer.WriteEndObject();
                writer.Flush();
            }
            writer.WriteEndArray();
            writer.WriteEndObject();
            writer.Flush();
            stdout.Flush();
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("sqldocgen-parser: " + ex.GetType().Name + ": " + ex.Message);
            return 2;
        }
    }

    // ------------------------------------------------------------------ parsers

    static Dictionary<int, Type> ParserTypes()
    {
        var found = new Dictionary<int, Type>();
        foreach (var t in typeof(TSqlParser).Assembly.GetExportedTypes())
        {
            if (!typeof(TSqlParser).IsAssignableFrom(t) || t.IsAbstract) continue;
            var name = t.Name;
            if (!name.StartsWith("TSql") || !name.EndsWith("Parser")) continue;
            var digits = name.Substring(4, name.Length - 10);
            if (int.TryParse(digits, out var n)) found[n] = t;
        }
        return found;
    }

    static Type PickParser(string name)
    {
        var all = ParserTypes();
        if (string.IsNullOrEmpty(name) || name.Equals("auto", StringComparison.OrdinalIgnoreCase))
            return all[all.Keys.Max()];
        var wanted = name.Trim();
        if (!wanted.EndsWith("Parser", StringComparison.OrdinalIgnoreCase)) wanted += "Parser";
        if (!wanted.StartsWith("TSql", StringComparison.OrdinalIgnoreCase)) wanted = "TSql" + wanted;
        var hit = typeof(TSqlParser).Assembly.GetExportedTypes()
            .FirstOrDefault(t => t.Name.Equals(wanted, StringComparison.OrdinalIgnoreCase) && typeof(TSqlParser).IsAssignableFrom(t));
        if (hit == null)
            throw new ArgumentException($"Unknown parser '{name}'. Available: " + string.Join(", ", all.OrderBy(p => p.Key).Select(p => p.Value.Name)));
        return hit;
    }

    static string ScriptDomVersion()
    {
        var asm = typeof(TSqlParser).Assembly;
        try
        {
            var info = FileVersionInfo.GetVersionInfo(asm.Location);
            var v = info.ProductVersion ?? info.FileVersion ?? asm.GetName().Version.ToString();
            var plus = v.IndexOf('+');
            return plus > 0 ? v.Substring(0, plus) : v;
        }
        catch
        {
            return asm.GetName().Version.ToString();
        }
    }

    // ------------------------------------------------------------------ serialisation

    static readonly HashSet<string> Skipped = new()
    {
        "ScriptTokenStream", "FirstTokenIndex", "LastTokenIndex",
        "StartOffset", "FragmentLength", "StartLine", "StartColumn",
    };

    // Computed conveniences on multi-part names duplicate the Identifiers list.
    static readonly HashSet<string> SkippedOnMultiPart = new()
    {
        "Count", "ServerIdentifier", "DatabaseIdentifier", "SchemaIdentifier", "BaseIdentifier", "ChildIdentifier",
    };

    static readonly Dictionary<Type, PropertyInfo[]> PropertyCache = new();

    static PropertyInfo[] PropertiesOf(Type type)
    {
        if (PropertyCache.TryGetValue(type, out var cached)) return cached;
        bool multiPart = typeof(MultiPartIdentifier).IsAssignableFrom(type);
        var props = type.GetProperties(BindingFlags.Public | BindingFlags.Instance)
            .Where(p => p.CanRead && p.GetIndexParameters().Length == 0)
            .Where(p => !Skipped.Contains(p.Name))
            .Where(p => !(multiPart && SkippedOnMultiPart.Contains(p.Name)))
            .OrderBy(p => p.MetadataToken)
            .ToArray();
        PropertyCache[type] = props;
        return props;
    }

    static void WriteNode(Utf8JsonWriter w, TSqlFragment node)
    {
        w.WriteStartObject();
        w.WriteString("$", node.GetType().Name);
        w.WritePropertyName("@");
        w.WriteStartArray();
        w.WriteNumberValue(node.StartOffset);
        w.WriteNumberValue(node.FragmentLength);
        w.WriteNumberValue(node.StartLine);
        w.WriteNumberValue(node.StartColumn);
        w.WriteEndArray();
        foreach (var p in PropertiesOf(node.GetType()))
        {
            object value;
            try { value = p.GetValue(node); }
            catch { continue; }
            if (value == null) continue;
            switch (value)
            {
                case TSqlFragment child:
                    w.WritePropertyName(p.Name);
                    WriteNode(w, child);
                    break;
                case string s:
                    w.WriteString(p.Name, s);
                    break;
                case bool b:
                    if (b) w.WriteBoolean(p.Name, true);
                    break;
                case Enum e:
                    w.WriteString(p.Name, e.ToString());
                    break;
                case int i:
                    w.WriteNumber(p.Name, i);
                    break;
                case long l:
                    w.WriteNumber(p.Name, l);
                    break;
                case IEnumerable list:
                    var items = list.Cast<object>().ToList();
                    if (items.Count == 0) break;
                    w.WritePropertyName(p.Name);
                    w.WriteStartArray();
                    foreach (var item in items)
                    {
                        switch (item)
                        {
                            case TSqlFragment f: WriteNode(w, f); break;
                            case string s2: w.WriteStringValue(s2); break;
                            case Enum e2: w.WriteStringValue(e2.ToString()); break;
                            case int i2: w.WriteNumberValue(i2); break;
                            default: w.WriteNullValue(); break;
                        }
                    }
                    w.WriteEndArray();
                    break;
                default:
                    break;
            }
        }
        w.WriteEndObject();
    }
}
