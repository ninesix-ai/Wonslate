// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using Wonslate.Sidecar;
using Xunit;

namespace LocalTranslator.Tests;

/// <summary>
/// SidecarManager 生命周期状态机测试。进程启动/健康探测用注入的假委托驱动，
/// 完全 hermetic：不拉起真实进程、不发真实 HTTP，只验证状态迁移与"失败不抛"契约。
/// 缺席/超时的真实兜底由 Rust 侧 is_available + 路由回落 demo 承担，这里只确保 .NET 侧不崩。
/// </summary>
public class SidecarManagerTests
{
    private static SidecarSpec Spec(string baseUrl, string exe = "sidecar.exe") =>
        new("argos", baseUrl, exe, "--port 11435", healthTimeoutMs: 500, pollIntervalMs: 10);

    // ── Start：健康探测成功 → Ready ────────────────────────────────

    [Fact]
    public void Start_HealthyOnFirstProbe_BecomesReady()
    {
        int launches = 0;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => { launches++; return true; },
            healthProbe: _ => true,
            sleep: _ => { });

        Assert.True(mgr.Start());
        Assert.Equal(SidecarState.Ready, mgr.State);
        Assert.Equal(1, launches);
    }

    [Fact]
    public void Start_BecomesHealthyOnThirdProbe_ReadyAfterPolling()
    {
        int probes = 0;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => ++probes >= 3,
            sleep: _ => { });

        Assert.True(mgr.Start());
        Assert.Equal(SidecarState.Ready, mgr.State);
        Assert.Equal(3, probes);
    }

    // ── Start：始终不健康 → Failed，且不抛 ────────────────────────

    [Fact]
    public void Start_NeverHealthy_BecomesFailed_AndDoesNotThrow()
    {
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => false,
            sleep: _ => { });

        // 超时窗口很小（healthTimeoutMs=500, poll=10），应在有限轮询后判失败
        Assert.False(mgr.Start());
        Assert.Equal(SidecarState.Failed, mgr.State);
    }

    [Fact]
    public void Start_ProbeThrows_IsTreatedAsUnhealthy_NotCrash()
    {
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => throw new System.Net.Http.HttpRequestException("connect refused"),
            sleep: _ => { });

        Assert.False(mgr.Start());
        Assert.Equal(SidecarState.Failed, mgr.State);
    }

    // ── 无 ExePath：视为复用外部已启动服务，跳过 launch 只探测 ──────

    [Fact]
    public void Start_NoExePath_SkipsLaunch_StillProbes()
    {
        int launches = 0;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435", exe: ""),   // 空 = 不拉起进程
            launch: _ => { launches++; return true; },
            healthProbe: _ => true,
            sleep: _ => { });

        Assert.True(mgr.Start());
        Assert.Equal(SidecarState.Ready, mgr.State);
        Assert.Equal(0, launches);   // 没有 exe 就不该尝试启动进程
    }

    // ── Stop：就绪后停止 → Stopped 且调用 kill ────────────────────

    [Fact]
    public void Stop_AfterReady_BecomesStopped_AndKillsProcess()
    {
        int kills = 0;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => true,
            sleep: _ => { },
            kill: _ => kills++);

        Assert.True(mgr.Start());
        mgr.Stop();
        Assert.Equal(SidecarState.Stopped, mgr.State);
        Assert.Equal(1, kills);
    }

    [Fact]
    public void Stop_WhenNotStarted_IsNoOp()
    {
        int kills = 0;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true, healthProbe: _ => true, sleep: _ => { },
            kill: _ => kills++);

        mgr.Stop();
        Assert.Equal(SidecarState.Stopped, mgr.State);
        Assert.Equal(0, kills);   // 从未启动，无需 kill
    }

    // ── 幂等 / 重试 ───────────────────────────────────────────────

    [Fact]
    public void Start_WhenAlreadyReady_IsIdempotent()
    {
        int launches = 0;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => { launches++; return true; },
            healthProbe: _ => true, sleep: _ => { });

        Assert.True(mgr.Start());
        Assert.True(mgr.Start());   // 再次 Start 直接返回，不重复拉起
        Assert.Equal(1, launches);
    }

    [Fact]
    public void Start_AfterFailed_CanRetry()
    {
        int probeResult = 0;   // 首轮失败，第二轮成功
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => probeResult > 0,
            sleep: _ => { });

        Assert.False(mgr.Start());
        Assert.Equal(SidecarState.Failed, mgr.State);

        probeResult = 1;               // 模拟 sidecar 随后起来了
        Assert.True(mgr.Start());
        Assert.Equal(SidecarState.Ready, mgr.State);
    }

    // ── 健康探测的 URL 约定：BaseUrl + /health ─────────────────────

    [Fact]
    public void Start_ProbesHealthEndpoint_OfBaseUrl()
    {
        string? probedUrl = null;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: url => { probedUrl = url; return true; },
            sleep: _ => { });

        Assert.True(mgr.Start());
        Assert.Equal("http://127.0.0.1:11435/health", probedUrl);
    }
}
