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

    // ---- liveness and readiness are two different verdicts ----------------------

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

    // ------------------------------------------------------------------ D73: who is answering
    //
    // A survivor of an earlier session answers /health exactly as well as the process this Start
    // launched. These cases pin that "someone replied" is no longer enough for Ready.

    [Fact]
    public void Start_OurPidAnswers_IsReadyAndSaysNothingStrange()
    {
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true, healthProbe: _ => true, sleep: _ => { },
            healthIdentity: _ => (true, 4242),
            ownedProcess: () => (4242, null));

        Assert.True(mgr.Start());
        Assert.Equal(SidecarState.Ready, mgr.State);
        Assert.Null(mgr.StartAnomaly);
    }

    [Fact]
    public void Start_AnotherPidAnswers_IsFailedAndTheAnomalyNamesBothSides()
    {
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true, healthProbe: _ => true, sleep: _ => { },
            healthIdentity: _ => (true, 999),
            ownedProcess: () => (4242, null));

        Assert.False(mgr.Start());
        Assert.Equal(SidecarState.Failed, mgr.State);
        // The message is the actionable part: "port taken by pid 999" tells the operator to stop
        // that process, where a bare Failed would tell them to restart one that is running fine.
        Assert.Contains("999", mgr.StartAnomaly);
        Assert.Contains("4242", mgr.StartAnomaly);
        // The UI assembles its Chinese hint from this, not from the English detail above.
        Assert.Equal(Identity.Foreign, mgr.LastIdentityVerdict);
    }

    [Fact]
    public void Start_OurProcessExitedWhileThePortAnswers_IsFailedAsExited()
    {
        // The exact production shape: the child could not bind the occupied port and left, while
        // the older instance keeps answering. Two different diagnoses from two different
        // observations, and neither may be reported as "the sidecar is ready".
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true, healthProbe: _ => true, sleep: _ => { },
            healthIdentity: _ => (true, null),
            ownedProcess: () => (4242, 1));

        Assert.False(mgr.Start());
        Assert.Equal(SidecarState.Failed, mgr.State);
        Assert.Contains("exited", mgr.StartAnomaly);
        Assert.Equal(Identity.Exited, mgr.LastIdentityVerdict);
    }

    [Fact]
    public void Start_OlderSidecarWithoutPid_StillWorksButRecordsThatItCouldNotTell()
    {
        // The paired non-firing case. Refusing here would take down every deployment that updated
        // the desktop but not the sidecar, over a field the operator cannot add by restarting.
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435"),
            launch: _ => true, healthProbe: _ => true, sleep: _ => { },
            readyProbe: _ => Readiness.Ready,
            healthIdentity: _ => (true, null),
            ownedProcess: () => (4242, null));

        Assert.True(mgr.Start());
        Assert.Equal(SidecarState.Ready, mgr.State);
        Assert.Contains("without a pid", mgr.StartAnomaly);
        // Kept as a log detail only: the shell must not warn about something that is working.
        Assert.Equal(Identity.Unproven, mgr.LastIdentityVerdict);
    }

    [Fact]
    public void Start_ExternalServiceMode_HasNothingToProveAndClaimsNothing()
    {
        // Empty ExePath is the documented "reuse what the operator started" mode (REQ-A4): no
        // launch, so no ownership claim, and no anomaly either.
        var mgr = new SidecarManager(
            Spec("http://127.0.0.1:11435", exe: ""),
            launch: _ => true, healthProbe: _ => true, sleep: _ => { },
            healthIdentity: _ => (true, 999),
            ownedProcess: () => (null, null));

        Assert.True(mgr.Start());
        Assert.Null(mgr.StartAnomaly);
        Assert.Null(mgr.LastIdentityVerdict);
    }

    [Fact]
    public void ClassifyIdentity_ADeadChildOutranksAForeignPid()
    {
        // Order is meaning: with both facts present, "our process died" is the diagnosis and
        // "pid 999 answered" is a detail. Swapping the branches would send the reader to compare
        // process ids instead of looking at their own exit code.
        var (label, detail) = SidecarManager.ClassifyIdentity(4242, 1, 999);
        Assert.Equal(Identity.Exited, label);
        Assert.Contains("code 1", detail);
        Assert.Contains("999", detail);
    }

    [Fact]
    public void IdentityVocabularyMatchesThePythonRuleItMirrors()
    {
        // Same repo, two languages, one contract (D72/D73). Read the labels out of the Python
        // source rather than restating them here, so adding a fifth verdict on one side turns the
        // other side red instead of quietly diverging.
        var dir = new System.IO.DirectoryInfo(System.AppContext.BaseDirectory);
        while (dir is not null && !System.IO.File.Exists(
                   System.IO.Path.Combine(dir.FullName, "script", "sidecar_identity.py")))
        {
            dir = dir.Parent;
        }
        Assert.NotNull(dir);        // the test assembly must stay inside the checkout
        var text = System.IO.File.ReadAllText(
            System.IO.Path.Combine(dir!.FullName, "script", "sidecar_identity.py"));

        var python = new System.Collections.Generic.SortedSet<string>();
        foreach (var line in text.Split('\n'))
        {
            var match = System.Text.RegularExpressions.Regex.Match(
                line, "^([A-Z_]+) = \"([a-z]+)\"");
            if (match.Success) python.Add(match.Groups[2].Value);
        }
        Assert.True(python.Count >= 4, $"parsed no labels from the Python rule: {python.Count}");

        // `silent` is the one C# does not model: Start only asks the identity question after its
        // own liveness probe already succeeded, so "nobody answered" is not reachable there.
        var csharp = new System.Collections.Generic.SortedSet<string>(
            System.Linq.Enumerable.Select(System.Enum.GetNames<Identity>(),
                                          name => name.ToLowerInvariant()));
        python.Remove("silent");
        Assert.Equal(string.Join(",", python), string.Join(",", csharp));
    }
}
