// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using Wonslate.Interop;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// EngineNative DTO deserialization tests (pure logic, no P/Invoke).
/// The DTO is the .NET parsing contract for Rust FFI JSON; drift here breaks the UI.
/// </summary>
public class EngineNativeDtoTests
{
    // ---- TranslateResponseDto (v2.0 full API)------------------------

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
        // missing output / error / message must not break deserialization
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
        // contract: malformed JSON must not throw through to the UI layer
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

    // ---- EngineResult (backward-compatible legacy API)------------------------------------

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
