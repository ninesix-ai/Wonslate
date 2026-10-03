// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.Diagnostics;
using System.Net;
using System.Net.Http;

namespace Wonslate.Sidecar;

/// <summary>Sidecar lifecycle state.</summary>
internal enum SidecarState
{
    Stopped,
    Ready,
    Failed,
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
}

/// <summary>
/// Manages one local sidecar endpoint (argos / madlad) through launch -> health polling -> ready/failed -> stop.
///
/// Key contracts:
///  - Any Start failure (process will not launch / health timeout / probe exception) returns false and NEVER throws,
///    transitioning to Failed; the Rust side then falls back to demo explicitly, so translation never breaks on an absent sidecar.
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

    // Guards _proc / _processOwned / _state: Start runs on a worker thread while the UI
    // thread (or Stop at shutdown) may read the state at any moment.
    private readonly object _gate = new();
    private Process? _proc;
    private bool _processOwned;
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
        Action<SidecarSpec>? kill = null)
    {
        _spec = spec;
        _launch = launch;
        _probe = healthProbe;
        _sleep = sleep;
        _kill = kill;
    }

    /// <summary>Production constructor: no injection, the built-in launcher and health probe are used.</summary>
    public SidecarManager(SidecarSpec spec)
        : this(spec, launch: null, healthProbe: null, sleep: null, kill: null)
    {
    }

    /// <summary>Build production instances for the argos / madlad endpoints.</summary>
    public static SidecarManager ForEndpoint(SidecarSpec spec) => new(spec);

    /// <summary>Start and wait for readiness. true = Ready; false = Failed (never throws).</summary>
    public bool Start()
    {
        if (State == SidecarState.Ready) return true;   // idempotent

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
            if (SafeProbe()) { SetState(SidecarState.Ready); return true; }
            if (Environment.TickCount64 >= deadline)
            {
                SetState(SidecarState.Failed);
                ReleaseOwnedProcess();   // do not leave a zombie process behind if it will not come up
                return false;
            }
            Sleep(_spec.pollIntervalMs);
        }
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

    private bool SafeProbe()
    {
        try { return _probe is { } ? _probe(_spec.HealthUrl) : DefaultProbe(_spec.HealthUrl); }
        catch { return false; }   // absent / refused / timeout -> treated as unhealthy, no crash
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
