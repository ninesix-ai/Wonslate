// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

namespace Wonslate.Sidecar;

/// <summary>
/// 从环境变量推导 argos / madlad 的 SidecarSpec。
///
/// 端口默认值必须与 Rust engine/sidecar.rs 一致（argos=11435、madlad=11436），
/// 否则 .NET 侧探测的端口与 Rust 引擎实际调用的端口会错位。env 读取器可注入，便于测试。
///
/// 环境变量约定：
///   LT_ARGOS_URL / LT_MADLAD_URL           覆盖 base url
///   LT_ARGOS_EXE / LT_MADLAD_EXE           sidecar 可执行路径（空=只探测外部已启动服务，不拉起进程）
///   LT_SIDECAR_HEALTH_TIMEOUT_MS / _POLL_MS 健康等待超时 / 轮询间隔
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

    private static SidecarSpec Build(
        string name, string urlKey, string defaultUrl, Func<string, string?> env)
    {
        var url = env(urlKey);
        var baseUrl = string.IsNullOrWhiteSpace(url) ? defaultUrl : url!.Trim();

        var exe = env($"LT_{name.ToUpperInvariant()}_EXE");
        var args = env($"LT_{name.ToUpperInvariant()}_ARGS") ?? "";

        var timeout = ParseInt(env("LT_SIDECAR_HEALTH_TIMEOUT_MS"), DefaultHealthTimeoutMs);
        var poll = ParseInt(env("LT_SIDECAR_POLL_MS"), DefaultPollMs);

        return new SidecarSpec(name, baseUrl, exe ?? "", args, timeout, poll);
    }

    private static int ParseInt(string? raw, int fallback) =>
        int.TryParse(raw, out var v) && v > 0 ? v : fallback;
}
