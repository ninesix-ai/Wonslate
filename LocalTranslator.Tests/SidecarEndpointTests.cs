// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System.Collections.Generic;
using Wonslate.Sidecar;
using Xunit;

namespace LocalTranslator.Tests;

/// <summary>
/// SidecarSpecFactory：从环境变量推导 argos/madlad 端点规格。纯逻辑、注入 env 读取器，
/// 无真实环境变量副作用。约束：baseUrl 必须与 Rust engine/sidecar.rs 的默认端口一致，
/// 否则 .NET 侧探测的端口与 Rust 引擎实际调用的端口会错位。
/// </summary>
public class SidecarEndpointTests
{
    private static SidecarSpec Argos(Dictionary<string, string> env) =>
        SidecarSpecFactory.Argos(key => env.TryGetValue(key, out var v) ? v : null);

    private static SidecarSpec Madlad(Dictionary<string, string> env) =>
        SidecarSpecFactory.Madlad(key => env.TryGetValue(key, out var v) ? v : null);

    [Fact]
    public void Argos_WithoutEnv_UsesDefaultArgosPort()
    {
        var spec = Argos(new Dictionary<string, string>());
        Assert.Equal("argos", spec.Name);
        Assert.Equal("http://127.0.0.1:11435", spec.BaseUrl);   // 与 Rust DEFAULT_ARGOS_URL 对齐
    }

    [Fact]
    public void Madlad_WithoutEnv_UsesDefaultMadladPort()
    {
        var spec = Madlad(new Dictionary<string, string>());
        Assert.Equal("madlad", spec.Name);
        Assert.Equal("http://127.0.0.1:11436", spec.BaseUrl);   // 与 Rust DEFAULT_MADLAD_URL 对齐
    }

    [Fact]
    public void Argos_UrlEnvOverridesDefault()
    {
        var spec = Argos(new Dictionary<string, string> { ["LT_ARGOS_URL"] = "http://127.0.0.1:19999" });
        Assert.Equal("http://127.0.0.1:19999", spec.BaseUrl);
    }

    [Fact]
    public void Madlad_UrlEnvOverridesDefault()
    {
        var spec = Madlad(new Dictionary<string, string> { ["LT_MADLAD_URL"] = "http://127.0.0.1:18888" });
        Assert.Equal("http://127.0.0.1:18888", spec.BaseUrl);
    }

    [Fact]
    public void EmptyUrlEnv_FallsBackToDefault()
    {
        var spec = Argos(new Dictionary<string, string> { ["LT_ARGOS_URL"] = "   " });
        Assert.Equal("http://127.0.0.1:11435", spec.BaseUrl);   // 空/空白视为未设置
    }

    [Fact]
    public void ExePath_DefaultsEmpty_MeaningProbeOnly()
    {
        // 默认不配 exe → 视为复用外部已启动服务（App 接线不强行拉起进程）
        var spec = Argos(new Dictionary<string, string>());
        Assert.Equal("", spec.ExePath);
    }

    [Fact]
    public void ExePath_ReadFromEnv_WhenProvided()
    {
        var spec = Argos(new Dictionary<string, string> { ["LT_ARGOS_EXE"] = "C:\\sidecar\\argos.exe" });
        Assert.Equal("C:\\sidecar\\argos.exe", spec.ExePath);
    }

    [Fact]
    public void HealthTimeout_ReadFromEnv()
    {
        var spec = Argos(new Dictionary<string, string> { ["LT_SIDECAR_HEALTH_TIMEOUT_MS"] = "1234" });
        Assert.Equal(1234, spec.healthTimeoutMs);
    }

    [Fact]
    public void HealthTimeout_BadValue_UsesDefault()
    {
        var spec = Argos(new Dictionary<string, string> { ["LT_SIDECAR_HEALTH_TIMEOUT_MS"] = "not-a-number" });
        Assert.Equal(5000, spec.healthTimeoutMs);   // 解析失败回落默认，不抛
    }

    [Fact]
    public void HealthUrl_IsDerivedFromBaseUrl()
    {
        var spec = Argos(new Dictionary<string, string> { ["LT_ARGOS_URL"] = "http://127.0.0.1:11435/" });
        Assert.Equal("http://127.0.0.1:11435/health", spec.HealthUrl);  // 去尾斜杠再拼
    }
}
