// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using Wonslate.Sidecar;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// SidecarManager lifecycle state-machine tests. Launch, health probe, readiness probe and
/// warm-up post all run on injected fake delegates, fully hermetic: no real process, no real
/// HTTP; only state transitions and the "the never-throw" contract are verified.
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

    // ---- D23: liveness and readiness are two different verdicts ----------------------

    [Fact]
    public void Start_EndpointAliveButModelCold_SettlesAtWarmingNotFailed()
    {
        // The old code called any /health answer Ready. The endpoint is up here and its
        // checkpoint is not, and that has to be a state of its own rather than a success.
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => true,
            sleep: _ => { },
            readyProbe: _ => Readiness.Warming);

        Assert.False(mgr.Start());               // not usable yet, which Start() admits
        Assert.Equal(SidecarState.Warming, mgr.State);
    }

    [Fact]
    public void Start_Warming_IsSettledAtOnceAndNeverTimesOutIntoAKill()
    {
        // The timeout branch is the one that calls ReleaseOwnedProcess(); reaching a
        // verdict from the first readiness answer means a slow model load cannot walk an
        // endpoint we just launched into Failed, and therefore cannot kill it.
        int sleeps = 0;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => true,
            sleep: _ => sleeps++,
            readyProbe: _ => Readiness.Warming);

        mgr.Start();
        Assert.Equal(0, sleeps);
        Assert.Equal(SidecarState.Warming, mgr.State);
    }

    [Fact]
    public void Start_WhenReadinessCannotTell_KeepsTheLivenessVerdict()
    {
        // An older sidecar has no /readyz at all. Treating "it cannot answer that question"
        // as a fault would regress every build that predates the endpoint.
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => true,
            sleep: _ => { },
            readyProbe: _ => Readiness.Unknown);

        Assert.True(mgr.Start());
        Assert.Equal(SidecarState.Ready, mgr.State);
    }

    [Fact]
    public void Start_WithoutAReadinessProbe_ProbesNothingAndTrustsLiveness()
    {
        // What every pre-existing test above relies on, made explicit: no delegate means
        // no /readyz traffic, so unit tests stay hermetic by construction.
        string? readyUrl = null;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => true,
            sleep: _ => { },
            readyProbe: null,
            warmPost: url => { readyUrl = url; return true; });

        Assert.True(mgr.Start());
        Assert.Null(readyUrl);
    }

    [Fact]
    public void Start_AskReadiness_AtTheReadyUrl_OfBaseUrl()
    {
        string? readyUrl = null;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => true,
            sleep: _ => { },
            readyProbe: url => { readyUrl = url; return Readiness.Ready; });

        Assert.True(mgr.Start());
        Assert.Equal("http://127.0.0.1:11435/readyz", readyUrl);
    }

    [Fact]
    public void Start_AfterWarming_DoesNotLaunchASecondProcess()
    {
        // Warming means "already brought up, model still loading"; re-entering Start()
        // must not spawn a rival sidecar for the same port.
        int launches = 0;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => { launches++; return true; },
            healthProbe: _ => true,
            sleep: _ => { },
            readyProbe: _ => Readiness.Warming);

        Assert.False(mgr.Start());
        Assert.False(mgr.Start());
        Assert.Equal(SidecarState.Warming, mgr.State);
        Assert.Equal(1, launches);
    }

    // ---- Warm-up: the way out of Warming -------------------------------------------

    [Fact]
    public void Warm_FromWarming_PostsWarmupAndAdvancesToReady()
    {
        string? warmUrl = null;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => true,
            sleep: _ => { },
            readyProbe: _ => Readiness.Warming,
            warmPost: url => { warmUrl = url; return true; });
        mgr.Start();

        Assert.True(mgr.Warm());
        Assert.Equal("http://127.0.0.1:11435/warmup", warmUrl);
        Assert.Equal(SidecarState.Ready, mgr.State);
    }

    [Fact]
    public void Warm_WhenAlreadyReady_IsANoOpSoTheShellDoesNotPostPerSentence()
    {
        // The shell reports the engine after every translation; once the endpoint is
        // resident that must not turn into an extra HTTP round trip per sentence.
        int posts = 0;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => true,
            sleep: _ => { },
            readyProbe: _ => Readiness.Ready,
            warmPost: _ => { posts++; return true; });
        mgr.Start();

        Assert.True(mgr.Warm());
        Assert.Equal(0, posts);
    }

    [Fact]
    public void Warm_WhenTheEndpointRefuses_StaysWarming_AndDoesNotThrow()
    {
        // A refused or failed warm-up is not evidence of death: the state must stay
        // Warming so a later real request (which loads the model itself) can still fix it.
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => true,
            sleep: _ => { },
            readyProbe: _ => Readiness.Warming,
            warmPost: _ => false);
        mgr.Start();

        Assert.False(mgr.Warm());
        Assert.Equal(SidecarState.Warming, mgr.State);
    }

    [Fact]
    public void Warm_WhenProbeThrows_IsHandledNotPropagated()
    {
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => true,
            sleep: _ => { },
            readyProbe: _ => Readiness.Warming,
            warmPost: _ => throw new System.Net.Http.HttpRequestException("connect refused"));
        mgr.Start();

        Assert.False(mgr.Warm());
        Assert.Equal(SidecarState.Warming, mgr.State);
    }

    [Fact]
    public void Warm_BeforeAnyStart_IsANoOpAndSaysSo()
    {
        bool posted = false;
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true,
            healthProbe: _ => true,
            sleep: _ => { },
            readyProbe: _ => Readiness.Ready,
            warmPost: _ => { posted = true; return true; });

        Assert.False(mgr.Warm());
        Assert.False(posted);          // nothing is up yet; do not knock on the port
        Assert.Equal(SidecarState.Stopped, mgr.State);
    }
}
