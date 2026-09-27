// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

namespace Wonslate.Sidecar;

/// <summary>
/// Derives the argos / madlad SidecarSpec from environment variables.
///
/// Port defaults must stay aligned with Rust engine/sidecar.rs
/// (argos=11435, madlad=11436); otherwise the port probed on the .NET side and
/// the port the Rust engine actually dials would silently diverge. The env
/// reader is injectable to keep this factory hermetic in tests.
///
/// Prefix chain per key (shared rule with the Rust config layer): legacy LT_*
/// wins for backward compatibility, then the brand spelling WONSLATE_*.
///
///   WONSLATE_ARGOS_URL / WONSLATE_MADLAD_URL           base-url override
///   WONSLATE_ARGOS_EXE / WONSLATE_MADLAD_EXE           sidecar executable path
///                                                       (empty = probe an already
///                                                       running external service,
///                                                       never spawn a process)
///   WONSLATE_SIDECAR_HEALTH_TIMEOUT_MS / _POLL_MS      health-wait timeout / poll interval
/// </summary>
internal static class SidecarSpecFactory
{
    private const int DefaultArgosPort = 11435;
    private const int DefaultMadladPort = 11436;
    private const int DefaultHealthTimeoutMs = 5000;
    private const int DefaultPollMs = 100;

    public static SidecarSpec Argos() => Argos(System.Environment.GetEnvironmentVariable);
    public static SidecarSpec Madlad() => Madlad(System.Environment.GetEnvironmentVariable);

    public static SidecarSpec Argos(Func<string, string?> env) =>
        Build("argos", "LT_ARGOS_URL", $"http://127.0.0.1:{DefaultArgosPort}", env);

    public static SidecarSpec Madlad(Func<string, string?> env) =>
        Build("madlad", "LT_MADLAD_URL", $"http://127.0.0.1:{DefaultMadladPort}", env);

    /// Resolve one logical key through the prefix chain: LT_* (legacy) first,
    /// then WONSLATE_* (brand). Empty/whitespace values count as unset.
    private static string? Env(Func<string, string?> env, string legacyKey, string brandKey) =>
        First(env(legacyKey), env(brandKey));

    private static string? First(params string?[] values)
    {
        foreach (var v in values)
            if (!string.IsNullOrWhiteSpace(v)) return v;
        return null;
    }

    private static SidecarSpec Build(
        string name, string legacyUrlKey, string defaultUrl, Func<string, string?> env)
    {
        string brandUrlKey = "WONSLATE_" + legacyUrlKey["LT_".Length..];
        var baseUrl = Env(env, legacyUrlKey, brandUrlKey) ?? defaultUrl;
        baseUrl = baseUrl.Trim();

        string upper = name.ToUpperInvariant();
        var exe = Env(env, $"LT_{upper}_EXE", $"WONSLATE_{upper}_EXE");
        var args = Env(env, $"LT_{upper}_ARGS", $"WONSLATE_{upper}_ARGS") ?? "";

        var timeout = ParseInt(
            Env(env, "LT_SIDECAR_HEALTH_TIMEOUT_MS", "WONSLATE_SIDECAR_HEALTH_TIMEOUT_MS"),
            DefaultHealthTimeoutMs);
        var poll = ParseInt(
            Env(env, "LT_SIDECAR_POLL_MS", "WONSLATE_SIDECAR_POLL_MS"),
            DefaultPollMs);

        return new SidecarSpec(name, baseUrl, exe ?? "", args, timeout, poll);
    }

    private static int ParseInt(string? raw, int fallback) =>
        int.TryParse(raw, out var v) && v > 0 ? v : fallback;
}
