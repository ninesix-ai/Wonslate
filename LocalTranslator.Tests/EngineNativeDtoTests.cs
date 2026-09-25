// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using Wonslate.Interop;
using Xunit;

namespace LocalTranslator.Tests;

/// <summary>
/// EngineNative DTO 反序列化测试（纯逻辑，不触发 P/Invoke）。
/// DTO 是 .NET 侧对 Rust FFI 返回 JSON 的解析契约，一旦偏差就影响 UI 展示。
/// </summary>
public class EngineNativeDtoTests
{
    // ── TranslateResponseDto（v2.0 全功能接口）────────────────────

    [Fact]
    public void TranslateResponseDto_DeserializesTmHit()
    {
        var json = """
            {"ok":true,"engine":"tm","source_lang":"zh","target_lang":"en",
             "input":"你好","output":"Hello","source":"tm_hit",
             "latency_ms":0,"confidence":0.95}
            """;
        var dto = EngineNative.TranslateResponseDto.FromJson(json);

        Assert.True(dto.Ok);
        Assert.Equal("tm", dto.Engine);
        Assert.Equal("zh", dto.SourceLang);
        Assert.Equal("en", dto.TargetLang);
        Assert.Equal("你好", dto.Input);
        Assert.Equal("Hello", dto.Output);
        Assert.Equal("tm_hit", dto.Source);
        Assert.Equal(0L, dto.LatencyMs);
        Assert.Equal(0.95f, dto.Confidence, 3);
        Assert.Null(dto.Error);
        Assert.Null(dto.Message);
    }

    [Fact]
    public void TranslateResponseDto_HandlesAiUpgradedWithMessage()
    {
        var json = """
            {"ok":true,"engine":"ollama-qwen","source_lang":"zh","target_lang":"en",
             "input":"x","output":"y","source":"ai_upgraded","latency_ms":2341,
             "confidence":0.95,"message":"upgraded"}
            """;
        var dto = EngineNative.TranslateResponseDto.FromJson(json);

        Assert.True(dto.Ok);
        Assert.Equal("ollama-qwen", dto.Engine);
        Assert.Equal("ai_upgraded", dto.Source);
        Assert.Equal(2341L, dto.LatencyMs);
        Assert.Equal("upgraded", dto.Message);
    }

    [Fact]
    public void TranslateResponseDto_NullOutputOnError()
    {
        var json = """
            {"ok":false,"engine":"ollama","source_lang":"zh","target_lang":"en",
             "input":"x","output":null,"source":"fallback","latency_ms":3,
             "confidence":0.0,"error":"ENGINE_FAILED","message":"timeout"}
            """;
        var dto = EngineNative.TranslateResponseDto.FromJson(json);

        Assert.False(dto.Ok);
        Assert.Null(dto.Output);
        Assert.Equal("ENGINE_FAILED", dto.Error);
        Assert.Equal("timeout", dto.Message);
    }

    [Fact]
    public void TranslateResponseDto_MissingOptionalFieldsDefaults()
    {
        // 缺 output / error / message，反序列化不能崩
        var json = """
            {"ok":true,"engine":"demo","source_lang":"en","target_lang":"zh",
             "input":"hello","source":"local","latency_ms":1,"confidence":0.6}
            """;
        var dto = EngineNative.TranslateResponseDto.FromJson(json);

        Assert.True(dto.Ok);
        Assert.Null(dto.Output);
        Assert.Null(dto.Error);
        Assert.Null(dto.Message);
    }

    [Fact]
    public void TranslateResponseDto_GarbageJsonReturnsParseError()
    {
        // 契约：非法 JSON 不能抛异常穿透到 UI 层
        var dto = EngineNative.TranslateResponseDto.FromJson("not json at all");

        Assert.False(dto.Ok);
        Assert.Equal("PARSE", dto.Error);
        Assert.False(string.IsNullOrEmpty(dto.Message));
    }

    [Fact]
    public void TranslateResponseDto_EmptyStringReturnsParseError()
    {
        var dto = EngineNative.TranslateResponseDto.FromJson("");
        Assert.False(dto.Ok);
        Assert.Equal("PARSE", dto.Error);
    }

    // ── EngineResult（向后兼容旧接口）──────────────────────────────

    [Fact]
    public void EngineResult_LegacyInterfaceStillWorks()
    {
        var json = """
            {"ok":true,"engine":"demo","source_lang":"en","target_lang":"zh",
             "input":"hello","output":"你好"}
            """;
        var r = EngineNative.EngineResult.FromJson(json);

        Assert.True(r.Ok);
        Assert.Equal("demo", r.Engine);
        Assert.Equal("你好", r.Output);
    }

    [Fact]
    public void EngineResult_ParseFailureDoesNotThrow()
    {
        var r = EngineNative.EngineResult.FromJson("{ broken json");
        Assert.False(r.Ok);
        Assert.Equal("PARSE", r.Error);
    }
}
