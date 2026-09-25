// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.Runtime.InteropServices;

namespace Wonslate.Interop;

/// <summary>
/// Rust 引擎 DLL（translator_engine.dll）的 C ABI 绑定。
/// 所有返回字符串由 Rust 侧分配，必须调用 tt_free_string 释放，防止内存泄漏。
/// </summary>
internal static partial class EngineNative
{
    private const string DllName = "translator_engine.dll";

    // ── 稳定接口（P0 签名不变）──────────────────────────────────────

    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_version();

    [LibraryImport(DllName, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr tt_translate(string engineId, string input, string langPair);

    [LibraryImport(DllName)]
    private static partial void tt_free_string(IntPtr ptr);

    // ── v2.0 新接口 ─────────────────────────────────────────────────

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

    // ── 公开封装 ────────────────────────────────────────────────────

    /// <summary>初始化 Rust 核心（应用启动时调一次）。</summary>
    public static void Init(string configJson = "{}")
    {
        IntPtr ptr = tt_init(configJson);
        if (ptr != IntPtr.Zero) tt_free_string(ptr);
    }

    /// <summary>关闭 Rust 核心（应用退出时调）。</summary>
    public static void Shutdown() => tt_shutdown();

    /// <summary>引擎版本号。</summary>
    public static string Version()
    {
        IntPtr raw = tt_version();
        try { return Marshal.PtrToStringUTF8(raw) ?? "unknown"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>基础翻译（向后兼容，不含 TM/蒸馏）。</summary>
    public static string Translate(string engineId, string input, string langPair)
    {
        IntPtr raw = tt_translate(engineId, input, langPair);
        try { return Marshal.PtrToStringUTF8(raw) ?? "{}"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>全功能翻译（含 TM + 路由 + 蒸馏，新代码推荐用此接口）。</summary>
    public static string TranslateFull(string requestJson)
    {
        IntPtr raw = tt_translate_full(requestJson);
        try { return Marshal.PtrToStringUTF8(raw) ?? "{}"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>查询 TM（不触发翻译，返回 TmEntry JSON 或 "null"）。</summary>
    public static string TmLookup(string text, string sourceLang, string targetLang)
    {
        IntPtr raw = tt_tm_lookup(text, sourceLang, targetLang);
        try { return Marshal.PtrToStringUTF8(raw) ?? "null"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>列出可用引擎（JSON 数组字符串）。</summary>
    public static string Engines()
    {
        IntPtr raw = tt_engines();
        try { return Marshal.PtrToStringUTF8(raw) ?? "[]"; }
        finally { tt_free_string(raw); }
    }

    /// <summary>健康检查（JSON 字符串）。</summary>
    public static string Health()
    {
        IntPtr raw = tt_health();
        try { return Marshal.PtrToStringUTF8(raw) ?? "{}"; }
        finally { tt_free_string(raw); }
    }

    // ── 向后兼容的 EngineResult（旧接口用）────────────────────────

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

    // ── v2.0 全功能响应 DTO ─────────────────────────────────────────

    public sealed record TranslateResponseDto(
        bool Ok, string Engine, string SourceLang, string TargetLang,
        string Input, string? Output, string Source,
        long LatencyMs, float Confidence, string? Error, string? Message)
    {
        private static readonly System.Text.Json.JsonSerializerOptions Opts = new()
        {
            PropertyNameCaseInsensitive = true,
            // Rust 端 JSON 是 snake_case（source_lang / latency_ms），
            // C# 是 PascalCase（SourceLang / LatencyMs），必须显式声明命名策略
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
