// tmdl-validate: load a TMDL folder with Microsoft's serializer (TOM) and report what it finds.
//
// Usage:  tmdl-validate <SemanticModel/definition folder>
// Output: one JSON object on stdout, always:
//   {"result": "succeeded"|"failed",
//    "tables": n, "measures": n, "relationships": n,          (only when the folder loaded)
//    "diagnostics": [{"severity": "error", "kind": "format"|"metadata"|"validation"|"tool",
//                     "file": "...", "line": n, "message": "..."}]}
// Exit code: 0 = loaded with no errors, 1 = errors found, 2 = bad usage.
//
// "format" errors are invalid TMDL text (indentation, unknown keyword, bad value); "metadata"
// errors are valid text describing an inconsistent model (e.g. a relationship to a missing
// column); "validation" errors come from Model.Validate() after loading.

using Microsoft.AnalysisServices.Tabular;
using Microsoft.AnalysisServices.Tabular.Tmdl;

if (args.Length != 1)
{
    Console.Error.WriteLine("usage: tmdl-validate <SemanticModel/definition folder>");
    return 2;
}

var diagnostics = new List<Dictionary<string, object?>>();
var output = new Dictionary<string, object?>();

static Dictionary<string, object?> Diagnostic(string kind, string message, string? file = null, int? line = null) =>
    new() { ["severity"] = "error", ["kind"] = kind, ["file"] = file, ["line"] = line, ["message"] = message };

try
{
    var database = TmdlSerializer.DeserializeDatabaseFromFolder(args[0]);
    var model = database.Model;
    output["tables"] = model.Tables.Count;
    output["measures"] = model.Tables.Sum(t => t.Measures.Count);
    output["relationships"] = model.Relationships.Count;
    foreach (var error in model.Validate().Errors)
    {
        diagnostics.Add(Diagnostic("validation", error.Message));
    }
}
catch (TmdlFormatException ex)
{
    diagnostics.Add(Diagnostic("format", ex.Message, ex.Document, ex.Line));  // message includes the line text
}
catch (TmdlSerializationException ex)
{
    diagnostics.Add(Diagnostic("metadata", ex.Message, ex.Document, ex.Line));
}
catch (Exception ex)
{
    diagnostics.Add(Diagnostic("tool", $"{ex.GetType().Name}: {ex.Message}"));
}

output["result"] = diagnostics.Count == 0 ? "succeeded" : "failed";
output["diagnostics"] = diagnostics;
Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(output));
return diagnostics.Count == 0 ? 0 : 1;
