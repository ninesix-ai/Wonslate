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

    [LibraryImport(DllName)]
    private static partial IntPtr tt_engines();

    [LibraryImport(DllName)]
    private static partial IntPtr tt_health();

    // ---- Public wrappers --------------------------------------------------------

    /// <summary>Initialize the Rust core (call once at app start).</summary>
    public static void Init(string configJson = "{}")
    {
        IntPtr ptr = tt_init(configJson);
        if (ptr != IntPtr.Zero) tt_free_string(ptr);
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
}
