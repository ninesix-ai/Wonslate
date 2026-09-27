// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System.Collections.Generic;
using Wonslate.Sidecar;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// SidecarSpecFactory: derives argos/madlad endpoint specs from environment
/// variables. Pure logic with an injected env reader, no real-env side effects.
/// Constraint: baseUrl defaults must match Rust engine/sidecar.rs ports,
/// otherwise the port probed on the .NET side and the port the Rust engine
/// actually dials would silently diverge.
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
        Assert.Equal("http://127.0.0.1:11435", spec.BaseUrl);   // aligns with the Rust DEFAULT_ARGOS_URL
    }

    [Fact]
    public void Madlad_WithoutEnv_UsesDefaultMadladPort()
    {
        var spec = Madlad(new Dictionary<string, string>());
        Assert.Equal("madlad", spec.Name);
        Assert.Equal("http://127.0.0.1:11436", spec.BaseUrl);   // aligns with the Rust DEFAULT_MADLAD_URL
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
        Assert.Equal("http://127.0.0.1:11435", spec.BaseUrl);   // empty / whitespace counts as unset
    }

    [Fact]
    public void ExePath_DefaultsEmpty_MeaningProbeOnly()
    {
        // No exe configured by default -> reuse an externally started service (the app wiring never force-launches)
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
        Assert.Equal(5000, spec.healthTimeoutMs);   // parse failure falls back to the default without throwing
    }

    [Fact]
    public void HealthUrl_IsDerivedFromBaseUrl()
    {
        var spec = Argos(new Dictionary<string, string> { ["LT_ARGOS_URL"] = "http://127.0.0.1:11435/" });
        Assert.Equal("http://127.0.0.1:11435/health", spec.HealthUrl);  // strip trailing slash before joining
    }

    [Fact]
    public void Argos_WonslateUrlEnvOverridesDefault()
    {
        // Brand spelling: WONSLATE_ARGOS_URL is honored like its LT_ predecessor.
        var spec = Argos(new Dictionary<string, string> { ["WONSLATE_ARGOS_URL"] = "http://127.0.0.1:19998" });
        Assert.Equal("http://127.0.0.1:19998", spec.BaseUrl);
    }

    [Fact]
    public void LegacyLtPrefixWinsOverWonslateForBackwardCompat()
    {
        // Documented prefix chain: LT_* (legacy) > WONSLATE_* (brand).
        var spec = Argos(new Dictionary<string, string>
        {
            ["LT_ARGOS_URL"] = "http://127.0.0.1:11111",
            ["WONSLATE_ARGOS_URL"] = "http://127.0.0.1:22222",
        });
        Assert.Equal("http://127.0.0.1:11111", spec.BaseUrl);
    }

    [Fact]
    public void ExePath_ReadFromWonslateEnv_WhenProvided()
    {
        var spec = Argos(new Dictionary<string, string> { ["WONSLATE_ARGOS_EXE"] = "C:\\sidecar\\argos.exe" });
        Assert.Equal("C:\\sidecar\\argos.exe", spec.ExePath);
    }

    [Fact]
    public void HealthTimeout_ReadFromWonslateEnv()
    {
        var spec = Argos(new Dictionary<string, string> { ["WONSLATE_SIDECAR_HEALTH_TIMEOUT_MS"] = "2345" });
        Assert.Equal(2345, spec.healthTimeoutMs);
    }
}
