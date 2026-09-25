// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.Diagnostics;
using System.Net;
using System.Net.Http;

namespace Wonslate.Sidecar;

/// <summary>sidecar 生命周期状态。</summary>
internal enum SidecarState
{
    Stopped,
    Ready,
    Failed,
}

/// <summary>一个 sidecar 端点的规格。BaseUrl 空串/ExePath 空串有专门语义（见 SidecarManager）。</summary>
internal sealed record SidecarSpec(
    string Name,
    string BaseUrl,
    string ExePath,
    string Args,
    int healthTimeoutMs = 5000,
    int pollIntervalMs = 100)
{
    /// <summary>健康探测地址：BaseUrl 去尾斜杠 + /health（与 Rust engine/sidecar.rs 约定一致）。</summary>
    public string HealthUrl => BaseUrl.TrimEnd('/') + "/health";
}

/// <summary>
/// 管理单个本机 sidecar 端点（argos / madlad）的拉起→健康轮询→就绪/失败→停止生命周期。
///
/// 关键契约：
///  - Start 失败（进程起不来 / 健康超时 / 探测异常）一律返回 false 且【不抛】，转入 Failed，
///    由 Rust 侧 is_available/路由缺席时显式回落 demo，翻译功能不因 sidecar 缺席而崩。
///  - ExePath 为空 → 视为"复用外部已启动的 sidecar"，跳过拉起、只做健康探测。
///  - 进程启动 / 健康探测 / 睡眠 均可注入，便于 hermetic 单元测试；默认用 Process + HttpClient。
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

    /// <summary>测试/自定义构造：注入各步委托。</summary>
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

    /// <summary>生产构造：内置 Process 拉起 + HttpClient 健康探测。</summary>
    public SidecarManager(SidecarSpec spec)
        : this(spec, launch: null!, healthProbe: null!, sleep: null!, kill: null!)
    {
    }

    /// <summary>为 argos / madlad 端点建生产实例。</summary>
    public static SidecarManager ForEndpoint(SidecarSpec spec) => new(spec);

    // 生产委托的落地实现（当未注入时使用）——用懒替换。
    static SidecarManager() { }

    /// <summary>启动并等待就绪。返回 true=Ready；false=Failed（不抛异常）。</summary>
    public bool Start()
    {
        if (State == SidecarState.Ready) return true;   // 幂等

        // 仅在配了可执行文件时才尝试拉起进程
        if (!string.IsNullOrWhiteSpace(_spec.ExePath))
        {
            bool ok = _launch is { } ? _launch(_spec) : LaunchProcess(_spec);
            if (!ok) { State = SidecarState.Failed; return false; }
            _processOwned = true;
        }

        // 轮询健康直到就绪或超时（探测异常按不健康处理，绝不外抛）
        long deadline = Environment.TickCount64 + _spec.healthTimeoutMs;
        while (true)
        {
            if (SafeProbe()) { State = SidecarState.Ready; return true; }
            if (Environment.TickCount64 >= deadline)
            {
                State = SidecarState.Failed;
                ReleaseOwnedProcess();   // 起不来就别留着僵尸进程
                return false;
            }
            Sleep(_spec.pollIntervalMs);
        }
    }

    /// <summary>停止：仅在确实由本实例拉起进程时才 kill；无论何种状态都转 Stopped。</summary>
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
        catch { return false; }   // 缺席/拒连/超时 → 视为不健康，不 crash
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
