// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Text;

namespace Wonslate.Sidecar;

/// <summary>Sidecar lifecycle state.</summary>
internal enum SidecarState
{
    Stopped,
    /// <summary>Answering, and able to serve a request without waiting on a model load.</summary>
    Ready,
    /// <summary>
    /// The process is up but its checkpoint is not resident yet.
    ///
    /// A distinct state, not a failure: the old code turned "answers /health" into
    /// Ready, which made the diagnostics line promise more than the endpoint could
    /// deliver, and treating warming as a timeout would kill a healthy process.
    /// </summary>
    Warming,
    Failed,
}

/// <summary>What one endpoint reports about serving right now. Three answers, because
/// "it cannot tell" is not the same fact as "not yet", and inventing a failure from the
/// former would break clients running an older sidecar that has no /readyz.</summary>
internal enum Readiness
{
    Ready,
    Warming,
    Unknown,
}

/// <summary>
/// Is who answered actually who we launched (defect D73). Mirrors `script/sidecar_identity.py`:
/// the desktop and a benchmark run share one sidecar contract, so the two sides must not drift
/// into different definitions of "good enough".
/// </summary>
internal enum Identity
{
    /// <summary>The responder is ours, or there was nothing to prove (external service mode).</summary>
    Ours,
    /// <summary>The responder named a pid that is not our process, while ours still runs.</summary>
    Foreign,
    /// <summary>Our process is gone yet the port answered: it cannot be ours.</summary>
    Exited,
    /// <summary>Something answered without publishing a pid (a sidecar older than D72).</summary>
    Unproven,
}

/// <summary>Spec for one sidecar endpoint. An empty BaseUrl or ExePath carries dedicated meaning (see SidecarManager).</summary>
internal sealed record SidecarSpec(
    string Name,
    string BaseUrl,
    string ExePath,
    string Args,
    int healthTimeoutMs = 5000,
    int pollIntervalMs = 100)
{
    /// <summary>Health endpoint: BaseUrl without a trailing slash + /health (matches the Rust engine/sidecar.rs convention).</summary>
    public string HealthUrl => BaseUrl.TrimEnd('/') + "/health";

    /// <summary>Readiness endpoint: is the model resident, not merely is the port listening.</summary>
    public string ReadyUrl => BaseUrl.TrimEnd('/') + "/readyz";

    /// <summary>Warm-up endpoint: ask the sidecar to load its model instead of spending a sentence on it.</summary>
    public string WarmUrl => BaseUrl.TrimEnd('/') + "/warmup";
}

/// <summary>
/// Manages one local sidecar endpoint (argos / madlad) through launch -> liveness polling
/// -> readiness (Ready or Warming) -> stop.
///
/// Key contracts:
///  - Any Start failure (process will not launch / liveness timeout / probe exception) returns false and NEVER throws,
///    transitioning to Failed; the Rust side then falls back to demo explicitly, so translation never breaks on an absent sidecar.
///  - Liveness and readiness are separate verdicts: answering /health brings the endpoint up, but only /readyz
///    decides Ready. A cold endpoint settles at Warming and its process is left running, because "still loading" is not
///    a fault and the old behaviour would have killed a healthy sidecar we had just launched.
///  - An empty ExePath means "reuse an externally started sidecar": launch is skipped, only the probe runs.
///  - Launch / probe / sleep are all injectable for hermetic unit tests; defaults use Process + HttpClient.
/// </summary>
internal sealed class SidecarManager
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromMilliseconds(1500) };

    private readonly SidecarSpec _spec;
    // Every step is nullable: null means "use the built-in Process / HttpClient implementation".
    // A production instance therefore injects nothing at all (no null! placeholders).
    private readonly Func<SidecarSpec, bool>? _launch;
    private readonly Func<string, bool>? _probe;
    private readonly Action<int>? _sleep;
    private readonly Action<SidecarSpec>? _kill;
    // Readiness is optional on purpose: with no delegate injected the manager keeps
    // the liveness-only verdict it started with, so unit tests stay hermetic and
    // nothing has to reach a socket to be exercised. Production injects both.
    private readonly Func<string, Readiness>? _ready;
    private readonly Func<string, bool>? _warm;

    // D73: which process is answering /health, and which process we launched. Injectable on
    // purpose -- the built-in versions need a real socket and a real child, and the state
    // machine that decides "Ready" must be testable without either.
    private readonly Func<string, (bool Answered, int? Pid)>? _healthIdentity;
    private readonly Func<(int? Pid, int? ExitCode)>? _ownedProcess;

    // Guards _proc / _processOwned / _state: Start runs on a worker thread while the UI
    // thread (or Stop at shutdown) may read the state at any moment.
    private readonly object _gate = new();
    private Process? _proc;
    private bool _processOwned;
    private string? _anomaly;
    private Identity? _lastIdentity;
    private SidecarState _state = SidecarState.Stopped;

    public SidecarState State { get { lock (_gate) { return _state; } } }

    /// <summary>Endpoint label (argos / madlad), used by the startup diagnostics line.</summary>
    public string Name => _spec.Name;

    /// <summary>Test / custom constructor: inject each step's delegate (null = built-in implementation).</summary>
    public SidecarManager(
        SidecarSpec spec,
        Func<SidecarSpec, bool>? launch,
        Func<string, bool>? healthProbe,
        Action<int>? sleep,
        Action<SidecarSpec>? kill = null,
        Func<string, Readiness>? readyProbe = null,
        Func<string, bool>? warmPost = null,
        Func<string, (bool Answered, int? Pid)>? healthIdentity = null,
        Func<(int? Pid, int? ExitCode)>? ownedProcess = null)
    {
        _spec = spec;
        _launch = launch;
        _probe = healthProbe;
        _sleep = sleep;
        _kill = kill;
        _ready = readyProbe;
        _warm = warmPost;
        _healthIdentity = healthIdentity;
        _ownedProcess = ownedProcess;
    }

    /// <summary>Production constructor: no injection, the built-in launcher, health probe, readiness probe and warm-up post are used.</summary>
    public SidecarManager(SidecarSpec spec)
        : this(spec, launch: null, healthProbe: null, sleep: null, kill: null,
               readyProbe: DefaultReadinessProbe, warmPost: DefaultWarmPost)
    {
    }

    /// <summary>Build production instances for the argos / madlad endpoints.</summary>
    public static SidecarManager ForEndpoint(SidecarSpec spec) => new(spec);

    /// <summary>
    /// Start and wait for readiness. true = Ready; false = Warming or Failed (never throws).
    /// </summary>
    public bool Start()
    {
        // Warming counts as "already brought up": re-entering here would launch a second
        // process for an endpoint whose model is simply still loading.
        if (State is SidecarState.Ready or SidecarState.Warming) return State == SidecarState.Ready;

        // Only try to launch a process when an executable is configured
        if (!string.IsNullOrWhiteSpace(_spec.ExePath))
        {
            bool ok = _launch is { } ? _launch(_spec) : LaunchProcess(_spec);
            if (!ok) { SetState(SidecarState.Failed); return false; }
            lock (_gate) { _processOwned = true; }
        }

        // Poll health until ready or timeout (probe exceptions count as unhealthy; never rethrown)
        long deadline = Environment.TickCount64 + _spec.healthTimeoutMs;
        while (true)
        {
            if (SafeProbe())
            {
                // Alive is not the same as usable, and usable is not the same as ours. A survivor
                // of an earlier session answers /health just as well as the process we launched a
                // second ago, so trusting the probe alone used to report Ready while the knobs this
                // launch was asked for had never taken effect (defect D73). Identity is settled
                // before any readiness verdict is published.
                var (who, why) = ObserveIdentity();
                lock (_gate)
                {
                    _anomaly = who == Identity.Ours ? null : why;
                    _lastIdentity = who == Identity.Ours ? null : who;
                }
                if (who is Identity.Foreign or Identity.Exited)
                {
                    // Something owns that port, and it is not ours. Failing here is the honest
                    // half: the state stops claiming a configuration that is not running, and the
                    // anomaly names the responder so the operator has something to act on. The
                    // process we launched is released, never the stranger's.
                    SetState(SidecarState.Failed);
                    ReleaseOwnedProcess();
                    return false;
                }
                // Alive is not the same as usable. Readiness is a second, separate
                // verdict, and a cold answer settles at Warming: failing there would run
                // ReleaseOwnedProcess() and kill a sidecar that is merely still loading.
                var settled = ObserveReadiness();
                SetState(settled);
                return settled == SidecarState.Ready;
            }
            if (Environment.TickCount64 >= deadline)
            {
                SetState(SidecarState.Failed);
                ReleaseOwnedProcess();   // do not leave a zombie process behind if it will not come up
                return false;
            }
            Sleep(_spec.pollIntervalMs);
        }
    }

    /// <summary>
    /// Ask this endpoint to load its model now, so the next request does not stall on a
    /// cold load. Returns whether it reported readiness afterwards.
    ///
    /// Never throws, and a failed warm-up changes nothing: an endpoint we could not wake
    /// is still not evidence that it died, so a Failed / Stopped state is never written here.
    /// </summary>
    public bool Warm()
    {
        if (State is SidecarState.Failed or SidecarState.Stopped) return false;
        // The shell calls this after every translation that names this engine, so a
        // resident endpoint must answer without another HTTP round trip per sentence.
        if (State == SidecarState.Ready) return true;
        if (!SafeWarm()) return State == SidecarState.Ready;
        SetState(SidecarState.Ready);
        return true;
    }

    /// <summary>One readiness question, mapped to a state. Without an injected probe the
    /// manager only knows liveness, which is exactly the behaviour it keeps.</summary>
    private SidecarState ObserveReadiness()
    {
        // No delegate injected: the endpoint's readiness is simply unknown to us, so the
        // manager keeps the liveness-only verdict it had before.
        if (_ready is null) return SidecarState.Ready;
        // "It cannot tell" (including an older sidecar with no /readyz, and a probe that
        // threw) is honoured as Ready rather than as a fault: inventing a failure there
        // would regress exactly the builds that predate this endpoint.
        return SafeReadinessProbe(_spec.ReadyUrl) == Readiness.Warming
            ? SidecarState.Warming
            : SidecarState.Ready;
    }

    /// <summary>Stop: kill only a process this instance really launched; every state transitions to Stopped.</summary>
    public void Stop()
    {
        bool owned;
        lock (_gate) { owned = _processOwned; }
        if (owned)
        {
            _kill?.Invoke(_spec);
            ReleaseOwnedProcess();
        }
        SetState(SidecarState.Stopped);
    }

    private void SetState(SidecarState next) { lock (_gate) { _state = next; } }

    /// <summary>
    /// Why the last Start refused to treat the endpoint as its own, or null when it did.
    /// Surfaced by the shell's diagnostics line: "not ours" and "not there" need different
    /// remedies, and a bare Failed state would tell the operator to restart something that is
    /// already running fine.
    /// </summary>
    public string? StartAnomaly { get { lock (_gate) { return _anomaly; } } }

    /// <summary>
    /// Which identity verdict the last Start reached, or null when it was `Ours`. Structured as
    /// well as textual on purpose: the diagnostics line is user-facing Chinese, and a sentence
    /// assembled from a verdict reads better than a logged English detail pasted into a label.
    /// The two failures that cost the user a wrong "ready" are Foreign and Exited (D73).
    /// </summary>
    public Identity? LastIdentityVerdict { get { lock (_gate) { return _lastIdentity; } } }

    /// <summary>One identity question, asked once per Start after the endpoint answered.</summary>
    private (Identity Label, string Detail) ObserveIdentity()
    {
        var (pid, exitCode) = _ownedProcess is { } ? _ownedProcess() : OwnedProcess();
        var (answered, answererPid) = _healthIdentity is { }
            ? _healthIdentity(_spec.HealthUrl)
            : DefaultHealthIdentity(_spec.HealthUrl);
        if (!answered)
        {
            // Reached only in a race (the probe said alive, this one did not). Not a verdict
            // about ownership, so it must not fail a working endpoint.
            return (Identity.Unproven, "the endpoint stopped answering between the two probes");
        }
        return ClassifyIdentity(pid, exitCode, answererPid);
    }

    /// <summary>
    /// The rule, as pure arithmetic: same four branches as `sidecar_identity.classify` in
    /// script/, kept in step by the tests on both sides.
    /// </summary>
    internal static (Identity Label, string Detail) ClassifyIdentity(
        int? procPid, int? exitCode, int? answererPid)
    {
        if (procPid is null)
            return (Identity.Ours, "no process was launched by us; an external service is taken as asked for");
        if (answererPid is not null && answererPid == procPid)
            return (Identity.Ours, $"answered by pid {answererPid}, the process we started");
        if (exitCode is not null)
            return (Identity.Exited, $"our process (pid {procPid}) exited with code {exitCode} while the "
                                     + $"port answered{(answererPid is int other ? $" by pid {other}" : " without identifying itself")}");
        if (answererPid is not null)
            return (Identity.Foreign, $"port answered by pid {answererPid}, not our process (pid {procPid})");
        return (Identity.Unproven, $"port answered without a pid (a sidecar older than D72?), our process "
                                   + $"pid {procPid} is still running");
    }

    /// <summary>Our launched process as (pid, exit code while dead), or (null, null) if we started none.</summary>
    private (int? Pid, int? ExitCode) OwnedProcess()
    {
        Process? proc;
        lock (_gate) { proc = _proc; }
        if (proc is null) return (null, null);
        try
        {
            return proc.HasExited ? ((int?)proc.Id, proc.ExitCode) : ((int?)proc.Id, null);
        }
        catch
        {
            // A process we cannot read is still a process we launched: report the pid and no
            // exit code rather than inventing a death we did not observe.
            return (proc.Id, null);
        }
    }

    /// <summary>
    /// GET /health and read `(answered, pid)`. `answered` is false on any transport failure or
    /// non-200; `pid` is null when the body does not publish one, which is reported as unknown
    /// rather than as a match (defect D73, and the field D72 added).
    /// </summary>
    private static (bool Answered, int? Pid) DefaultHealthIdentity(string url)
    {
        try
        {
            using var resp = Http.GetAsync(url).GetAwaiter().GetResult();
            if (resp.StatusCode != HttpStatusCode.OK) return (false, null);
            using var doc = System.Text.Json.JsonDocument.Parse(
                resp.Content.ReadAsStringAsync().GetAwaiter().GetResult());
            if (doc.RootElement.TryGetProperty("pid", out var element)
                    && element.TryGetInt64(out var value))
                return (true, (int)value);
            return (true, null);
        }
        catch
        {
            return (false, null);
        }
    }

    private bool SafeProbe()
    {
        try { return _probe is { } ? _probe(_spec.HealthUrl) : DefaultProbe(_spec.HealthUrl); }
        catch { return false; }   // absent / refused / timeout -> treated as unhealthy, no crash
    }

    private Readiness SafeReadinessProbe(string url)
    {
        try { return _ready is { } ? _ready(url) : DefaultReadinessProbe(url); }
        catch { return Readiness.Unknown; }   // cannot classify -> never escalated to a failure
    }

    private bool SafeWarm()
    {
        try { return _warm is { } ? _warm(_spec.WarmUrl) : DefaultWarmPost(_spec.WarmUrl); }
        catch { return false; }   // unreachable sidecar: no state change, no throw
    }

    private void Sleep(int ms)
    {
        if (_sleep is { }) _sleep(ms);
        else Thread.Sleep(ms);
    }

    private bool LaunchProcess(SidecarSpec s)
    {
        try
        {
            var psi = new ProcessStartInfo(s.ExePath, s.Args)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            var proc = Process.Start(psi);
            if (proc is null) return false;
            lock (_gate) { _proc = proc; }
            return true;
        }
        catch
        {
            return false;
        }
    }

    private bool DefaultProbe(string url)
    {
        try
        {
            using var resp = Http.GetAsync(url).GetAwaiter().GetResult();
            return resp.StatusCode == HttpStatusCode.OK;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// Readiness by HTTP verb semantics: 200 is resident, 503 is "up but still loading"
    /// (the sidecar's own contract for warming), anything else - including 404 on an
    /// older build and a transport failure - is Unknown rather than a verdict.
    /// </summary>
    private static Readiness DefaultReadinessProbe(string url)
    {
        try
        {
            using var resp = Http.GetAsync(url).GetAwaiter().GetResult();
            return resp.StatusCode switch
            {
                HttpStatusCode.OK => Readiness.Ready,
                HttpStatusCode.ServiceUnavailable => Readiness.Warming,
                _ => Readiness.Unknown,
            };
        }
        catch
        {
            return Readiness.Unknown;
        }
    }

    /// <summary>POST an empty body: warming takes no arguments for the checkpoint tiers.</summary>
    private static bool DefaultWarmPost(string url)
    {
        try
        {
            using var content = new StringContent("{}", Encoding.UTF8, "application/json");
            using var resp = Http.PostAsync(url, content).GetAwaiter().GetResult();
            return resp.StatusCode == HttpStatusCode.OK;
        }
        catch
        {
            return false;
        }
    }

    private void ReleaseOwnedProcess()
    {
        Process? proc;
        lock (_gate)
        {
            proc = _proc;
            _proc = null;
            _processOwned = false;
        }
        try
        {
            if (proc is { } && !proc.HasExited) proc.Kill(entireProcessTree: true);
            proc?.Dispose();
        }
        catch
        {
            // Best effort only: an already-exited process needs no cleanup, and a failed
            // kill must not turn shutdown into a crash.
        }
    }
}
