// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using Wonslate.Sidecar;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// SidecarManager lifecycle state-machine tests. Launch and health probe run on injected fake delegates,
/// fully hermetic: no real process, no real HTTP; only state transitions and the"the never-throw"contract are verified.
/// Real absence / timeout fallback is the Rust side's job (is_available + routing to demo); here we only assert the .NET side never crashes.
/// </summary>
public class SidecarManagerTests
{
    private static SidecarSpec Spec(string baseUrl, string exe = "sidecar.exe") =>
        new("argos", baseUrl, exe, "--port 11435", healthTimeoutMs: 500, pollIntervalMs: 10);

    // ---- Start: health probe succeeds -> Ready --------------------------------

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

    // ---- Start: never healthy -> Failed, without throwing ------------------------

    [Fact]
    public void Start_NeverHealthy_BecomesFailed_AndDoesNotThrow()
    {
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => false,
            sleep: _ => { });

        // a tiny timeout window (healthTimeoutMs=500, poll=10) must fail after a bounded number of polls
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

    // ---- No ExePath: reuse an external service, skip launch and only probe ------------

    [Fact]
    public void Start_NoExePath_SkipsLaunch_StillProbes()
    {
        int launches = 0;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435", exe: ""),   // empty = never launch
            launch: _ => { launches++; return true; },
            healthProbe: _ => true,
            sleep: _ => { });

        Assert.True(mgr.Start());
        Assert.Equal(SidecarState.Ready, mgr.State);
        Assert.Equal(0, launches);   // without an exe no launch should be attempted
    }

    // ---- Stop: stopping a ready sidecar -> Stopped with kill called ------------------------

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
        Assert.Equal(0, kills);   // never launched, nothing to kill
    }

    // ---- Idempotence / retry ------------------------------------------------------

    [Fact]
    public void Start_WhenAlreadyReady_IsIdempotent()
    {
        int launches = 0;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => { launches++; return true; },
            healthProbe: _ => true, sleep: _ => { });

        Assert.True(mgr.Start());
        Assert.True(mgr.Start());   // a second Start returns straight away, no relaunch
        Assert.Equal(1, launches);
    }

    [Fact]
    public void Start_AfterFailed_CanRetry()
    {
        int probeResult = 0;   // first round fails, second round succeeds
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => probeResult > 0,
            sleep: _ => { });

        Assert.False(mgr.Start());
        Assert.Equal(SidecarState.Failed, mgr.State);

        probeResult = 1;               // the sidecar comes up shortly after
        Assert.True(mgr.Start());
        Assert.Equal(SidecarState.Ready, mgr.State);
    }

    // ---- Health-probe URL convention: BaseUrl + /health --------------------------

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
