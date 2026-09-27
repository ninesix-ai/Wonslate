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
    private readonly Func<SidecarSpec, bool> _launch;
    private readonly Func<string, bool> _probe;
    private readonly Action<int> _sleep;
    private readonly Action<SidecarSpec> _kill;

    private Process? _proc;
    private bool _processOwned;

    public SidecarState State { get; private set; } = SidecarState.Stopped;

    /// <summary>Test / custom constructor: inject each step's delegate.</summary>
    public SidecarManager(
        SidecarSpec spec,
        Func<SidecarSpec, bool> launch,
        Func<string, bool> healthProbe,
        Action<int> sleep,
        Action<SidecarSpec>? kill = null)
    {
        _spec = spec;
        _launch = launch;
        _probe = healthProbe;
        _sleep = sleep;
        _kill = kill ?? (_ => { });
    }

    /// <summary>Production constructor: built-in Process launch + HttpClient health probe.</summary>
    public SidecarManager(SidecarSpec spec)
        : this(spec, launch: null!, healthProbe: null!, sleep: null!, kill: null!)
    {
    }

    /// <summary>Build production instances for the argos / madlad endpoints.</summary>
    public static SidecarManager ForEndpoint(SidecarSpec spec) => new(spec);

    // Concrete production delegates (used when nothing was injected) -- swapped lazily.
    static SidecarManager() { }

    /// <summary>Start and wait for readiness. true = Ready; false = Failed (never throws).</summary>
    public bool Start()
    {
        if (State == SidecarState.Ready) return true;   // idempotent

        // Only try to launch a process when an executable is configured
        if (!string.IsNullOrWhiteSpace(_spec.ExePath))
        {
            bool ok = _launch is { } ? _launch(_spec) : LaunchProcess(_spec);
            if (!ok) { State = SidecarState.Failed; return false; }
            _processOwned = true;
        }

        // Poll health until ready or timeout (probe exceptions count as unhealthy; never rethrown)
        long deadline = Environment.TickCount64 + _spec.healthTimeoutMs;
        while (true)
        {
            if (SafeProbe()) { State = SidecarState.Ready; return true; }
            if (Environment.TickCount64 >= deadline)
            {
                State = SidecarState.Failed;
                ReleaseOwnedProcess();   // do not leave a zombie process behind if it will not come up
                return false;
            }
            Sleep(_spec.pollIntervalMs);
        }
    }

    /// <summary>Stop: kill only a process this instance really launched; every state transitions to Stopped.</summary>
    public void Stop()
    {
        if (_processOwned)
        {
            _kill?.Invoke(_spec);
            ReleaseOwnedProcess();
        }
        State = SidecarState.Stopped;
    }

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
            _proc = Process.Start(psi);
            return _proc is { };
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
        try
        {
            if (_proc is { } && !_proc.HasExited) _proc.Kill(entireProcessTree: true);
            _proc?.Dispose();
        }
        catch { /* 尽力回收，忽略 */ }
        finally { _proc = null; _processOwned = false; }
    }
}
