// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.Runtime.InteropServices;

namespace Wonslate.Interop;

/// <summary>
/// C ABI bindings for the Rust engine DLL (translator_engine.dll).
/// Every returned string is allocated on the Rust side and must be released via tt_free_string to avoid leaks.
/// </summary>
internal static partial class EngineNative
{
    private const string DllName = "translator_engine.dll";

    // ---- Stable API (P0 signatures unchanged)--------------------------------------------

    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_version();

    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_translate(string engineId, string input, string langPair);

    [LibraryImport(DllName)]
    private static partial void tt_free_string(IntPtr ptr);

    // ---- v2.0 API --------------------------------------------------

    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_init(string configJson);

    [LibraryImport(DllName)]
    private static partial void tt_shutdown();

    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_translate_full(string requestJson);

    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_tm_lookup(string text, string sourceLang, string targetLang);

    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_tm_put(string entryJson);

    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_tm_flag_bad(string text, string sourceLang, string targetLang);

    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_tm_list(string sourceLang, string targetLang, uint limit);

    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_glossary_list(string sourceLang, string targetLang);

    // S11: domain-scoped glossary fetch. The Rust side returns generic rows ∪
    // the requested specific domain; empty `domain` behaves like the v1 call.
    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_glossary_list_with_domain(
        string sourceLang, string targetLang, string domain, uint limit);

    // S11: one-shot seed pack importer. Payload shape is documented in
    // translator-engine/src/lib.rs::tt_glossary_import_pack.
    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_glossary_import_pack(string packJson);

    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_glossary_upsert(string entryJson);

    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_glossary_delete(string sourceTerm, string sourceLang, string targetLang);

    [LibraryImport(DllName)]
    private static partial IntPtr tt_config_json();

    [LibraryImport(DllName)]
    private static partial IntPtr tt_engines();

    [LibraryImport(DllName)]
    private static partial IntPtr tt_health();

    // ---- Public wrappers --------------------------------------------------------

    /// <summary>Initialize the Rust core (call once at app start). The ack is returned so a
    /// failed initialization can be surfaced in the UI instead of being swallowed.</summary>
    public static AckDto Init(string configJson = "{}")
    {
        IntPtr ptr = tt_init(configJson);
        try { return AckDto.FromJson(Marshal.PtrToStringUTF8(ptr) ?? "{}"); }
        finally { if (ptr != IntPtr.Zero) tt_free_string(ptr); }
    }

    /// <summary>Shut the Rust core down (call at app exit).</summary>
    public static void Shutdown() => tt_shutdown();

    /// <summary>Engine version string.</summary>
    public static string Version()
    {
        IntPtr raw = tt_version();
        try { return Marshal.PtrToStringUTF8(raw) ?? "unknown"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>Basic translation (backward compatible; no TM / distillation).</summary>
    public static string Translate(string engineId, string input, string langPair)
    {
        IntPtr raw = tt_translate(engineId, input, langPair);
        try { return Marshal.PtrToStringUTF8(raw) ?? "{}"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>Full-featured translation (TM + routing + distillation; preferred by new code).</summary>
    public static string TranslateFull(string requestJson)
    {
        IntPtr raw = tt_translate_full(requestJson);
        try { return Marshal.PtrToStringUTF8(raw) ?? "{}"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>Look up the TM (no translation triggered; returns TmEntry JSON or "null").</summary>
    public static string TmLookup(string text, string sourceLang, string targetLang)
    {
        IntPtr raw = tt_tm_lookup(text, sourceLang, targetLang);
        try { return Marshal.PtrToStringUTF8(raw) ?? "null"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>Write one translation pair into the TM (JSON ack).</summary>
    public static string TmPut(string entryJson)
    {
        IntPtr raw = tt_tm_put(entryJson);
        try { return Marshal.PtrToStringUTF8(raw) ?? "{}"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>Mark one TM entry bad: it stops being served and disappears from the list (JSON ack).</summary>
    public static string TmFlagBad(string text, string sourceLang, string targetLang)
    {
        IntPtr raw = tt_tm_flag_bad(text, sourceLang, targetLang);
        try { return Marshal.PtrToStringUTF8(raw) ?? "{}"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>List the active TM entries of one language pair (JSON array string).</summary>
    public static string TmList(string sourceLang, string targetLang, uint limit = 200)
    {
        IntPtr raw = tt_tm_list(sourceLang, targetLang, limit);
        try { return Marshal.PtrToStringUTF8(raw) ?? "[]"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>List the glossary terms of one language pair (JSON array string).</summary>
    public static string GlossaryList(string sourceLang, string targetLang)
    {
        IntPtr raw = tt_glossary_list(sourceLang, targetLang);
        try { return Marshal.PtrToStringUTF8(raw) ?? "[]"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>
    /// S11: list the glossary terms for one (source, target, domain) triple.
    /// Passing an empty `domain` returns only generic rows (same set the v1
    /// wrapper sees on a pre-S11 install); a named domain returns generic ∪
    /// that domain, with the specific row replacing an identically-named
    /// generic one.
    /// </summary>
    public static string GlossaryList(string sourceLang, string targetLang, string domain, uint limit)
    {
        IntPtr raw = tt_glossary_list_with_domain(sourceLang, targetLang, domain, limit);
        try { return Marshal.PtrToStringUTF8(raw) ?? "[]"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>
    /// S11: import one seed pack in a single call. Returns the FFI ack JSON with
    /// `{ok, imported}` on success, `{ok:false, error, message}` on a schema
    /// rejection. Callers pass the pack text through verbatim; the Rust layer
    /// enforces the mandatory pack-level `domain` / `source_lang` / `target_lang`.
    /// </summary>
    public static string GlossaryImportPack(string packJson)
    {
        IntPtr raw = tt_glossary_import_pack(packJson);
        try { return Marshal.PtrToStringUTF8(raw) ?? "{}"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>Insert or update one glossary term (JSON ack).</summary>
    public static string GlossaryUpsert(string entryJson)
    {
        IntPtr raw = tt_glossary_upsert(entryJson);
        try { return Marshal.PtrToStringUTF8(raw) ?? "{}"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>Delete one glossary term by key (JSON ack).</summary>
    public static string GlossaryDelete(string sourceTerm, string sourceLang, string targetLang)
    {
        IntPtr raw = tt_glossary_delete(sourceTerm, sourceLang, targetLang);
        try { return Marshal.PtrToStringUTF8(raw) ?? "{}"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>
    /// The routing that is actually in force, plus which files and environment variables
    /// produced it (JSON object). Used by the settings page to show the effective value
    /// rather than the value the user thinks they set.
    /// </summary>
    public static string ConfigJson()
    {
        IntPtr raw = tt_config_json();
        try { return Marshal.PtrToStringUTF8(raw) ?? "{}"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>List available engines (JSON array string).</summary>
    public static string Engines()
    {
        IntPtr raw = tt_engines();
        try { return Marshal.PtrToStringUTF8(raw) ?? "[]"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>Health check (JSON string).</summary>
    public static string Health()
    {
        IntPtr raw = tt_health();
        try { return Marshal.PtrToStringUTF8(raw) ?? "{}"; }
        finally { tt_free_string(raw); }
    }

    // ---- Backward-compatible EngineResult (legacy API)------------------------

    public sealed record EngineResult(
        bool Ok, string Engine, string SourceLang, string TargetLang,
        string Input, string? Output, string? Error, string? Message)
    {
        public static EngineResult FromJson(string json)
        {
            try
            {
                using var doc = System.Text.Json.JsonDocument.Parse(json);
                var root = doc.RootElement;
                return new EngineResult(
                    root.TryGetProperty("ok", out var ok) && ok.GetBoolean(),
                    GetS(root, "engine"), GetS(root, "source_lang"),
                    GetS(root, "target_lang"), GetS(root, "input"),
                    GetNs(root, "output"), GetNs(root, "error"), GetNs(root, "message"));
            }
            catch
            {
                return new EngineResult(false, "unknown", "", "", json, null, "PARSE",
                    "failed to parse engine response");
            }
        }

        private static string GetS(System.Text.Json.JsonElement r, string n) =>
            r.TryGetProperty(n, out var v) ? v.GetString() ?? "" : "";

        private static string? GetNs(System.Text.Json.JsonElement r, string n) =>
            r.TryGetProperty(n, out var v) &&
            v.ValueKind == System.Text.Json.JsonValueKind.String ? v.GetString() : null;
    }

    // ---- v2.0 full-feature response DTO ------------------------------------------

    public sealed record TranslateResponseDto(
        bool Ok, string Engine, string SourceLang, string TargetLang,
        string Input, string? Output, string Source,
        long LatencyMs, float Confidence, string? Error, string? Message)
    {
        private static readonly System.Text.Json.JsonSerializerOptions Opts = new()
        {
            PropertyNameCaseInsensitive = true,
            // Rust's JSON is snake_case (source_lang / latency_ms),
            // while C# is PascalCase (SourceLang / LatencyMs); the naming policy must be declared explicitly
            PropertyNamingPolicy = System.Text.Json.JsonNamingPolicy.SnakeCaseLower,
            DictionaryKeyPolicy   = System.Text.Json.JsonNamingPolicy.SnakeCaseLower,
        };

        public static TranslateResponseDto FromJson(string json)
        {
            try
            {
                return System.Text.Json.JsonSerializer.Deserialize<TranslateResponseDto>(
                    json, Opts) ?? new TranslateResponseDto(
                    false, "unknown", "", "", "", null, "fallback",
                    0, 0, "PARSE", "failed to deserialize");
            }
            catch (Exception ex)
            {
                return new TranslateResponseDto(
                    false, "unknown", "", "", "", null, "fallback",
                    0, 0, "PARSE", ex.Message);
            }
        }
    }

    /// <summary>Shared JSON options for the Rust snake_case contract. CJK text stays
    /// literal instead of \uXXXX-escaped: the Rust side reads UTF-8 either way, and the
    /// stored TM / glossary files stay human-readable.</summary>
    internal static readonly System.Text.Json.JsonSerializerOptions SnakeCase = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = System.Text.Json.JsonNamingPolicy.SnakeCaseLower,
        DictionaryKeyPolicy   = System.Text.Json.JsonNamingPolicy.SnakeCaseLower,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };
}

// ---- Write-endpoint ack / management DTOs ----------------------------------------
// Top-level (not nested in EngineNative) because the ViewModel exposes them as public
// properties for WPF binding, while the interop class itself stays internal.

/// <summary>Response of the write-only endpoints: {"ok":true} or {"ok":false,"error":..,"message":..}.</summary>
public sealed record AckDto(bool Ok, string? Error, string? Message)
{
    public static AckDto FromJson(string json)
    {
        try
        {
            return System.Text.Json.JsonSerializer.Deserialize<AckDto>(json, EngineNative.SnakeCase)
                ?? new AckDto(false, "PARSE", "empty ack");
        }
        catch (Exception ex)
        {
            return new AckDto(false, "PARSE", ex.Message);
        }
    }
}

/// <summary>One TM entry as reported by tt_tm_list / tt_tm_lookup.</summary>
public sealed record TmEntryDto(
    string SourceText, string SourceLang, string TargetText, string TargetLang,
    string Engine, float Quality, uint HitCount, string Domain)
{
    public static IReadOnlyList<TmEntryDto> ListFromJson(string json)
    {
        try
        {
            return System.Text.Json.JsonSerializer.Deserialize<List<TmEntryDto>>(json, EngineNative.SnakeCase)
                ?? new List<TmEntryDto>();
        }
        catch
        {
            return Array.Empty<TmEntryDto>();
        }
    }
}

/// <summary>One glossary term as reported by tt_glossary_list.</summary>
/// <param name="Source">Provenance (N-08): "distill" / "manual" / "tm"; null for records
/// written before the field existed. Machine-extracted terms are the ones worth reviewing.</param>
public sealed record GlossaryEntryDto(
    string SourceTerm, string SourceLang, string TargetTerm, string TargetLang,
    float Confidence, uint Frequency, string Domain, string? Source)
{
    public static IReadOnlyList<GlossaryEntryDto> ListFromJson(string json)
    {
        try
        {
            return System.Text.Json.JsonSerializer.Deserialize<List<GlossaryEntryDto>>(json, EngineNative.SnakeCase)
                ?? new List<GlossaryEntryDto>();
        }
        catch
        {
            return Array.Empty<GlossaryEntryDto>();
        }
    }
}
