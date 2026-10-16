// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.Text;
using System.Threading.Tasks;
using System.Windows;
using Wonslate.Interop;
using Wonslate.Sidecar;
using Wonslate.ViewModels;

namespace Wonslate;

public partial class App : Application
{
    // Process / lifecycle management for the Phase 2 sidecar engines (argos realtime slot / madlad rare fallback).
    // Not-ready / absent never blocks startup: the Rust router falls back to demo explicitly, translation keeps working.
    private SidecarManager? _argos;
    private SidecarManager? _madlad;

    private MainWindow? _window;
    private MainViewModel? _vm;
    private string _engineLine = "引擎状态未知";
    private readonly object _gate = new();

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        // Initialize the Rust core (open the TM store, load config, start the distill thread).
        // The ack is kept: a failed init has to reach the UI rather than disappear.
        var init = EngineNative.Init();
        _engineLine = init.Ok
            ? $"引擎已就绪 {EngineNative.Version()}"
            : $"引擎初始化失败：{init.Error} {init.Message}";

        // The shell owns the ViewModel so it can push startup diagnostics into it,
        // and supplies the voice panel with the real audio player.
        _vm = new MainViewModel(new VoiceViewModel(playWav: PlayWavFile));
        // The UI per-attempt timeout is env-overridable (LT_ > WONSLATE_ prefix chain) so an
        // operator can raise it alongside a larger *_SIDECAR_TIMEOUT_MS or a queue; absent it
        // stays at the shipped default. This is a .NET-side read only, not a Rust engine knob.
        _vm.TranslateTimeout = MainViewModel.ResolveTranslateTimeout(System.Environment.GetEnvironmentVariable);
        // The engine named in each response decides which endpoint gets warmed;
        // the managers are created below, so this must tolerate them being absent.
        _vm.OnEngineUsed = WarmEndpointFor;
        _window = new MainWindow(_vm);
        _window.Show();
        PublishDiagnostics();

        // Bring up / probe sidecars in the background; never block UI startup; failures only set state, never throw.
        _argos = new SidecarManager(SidecarSpecFactory.Argos());
        _madlad = new SidecarManager(SidecarSpecFactory.Madlad());
        StartSidecar(_argos);
        StartSidecar(_madlad);
    }

    /// <summary>
    /// Play a WAV through the system player. System.Media is part of the desktop
    /// framework, so voice output needs no extra audio dependency (input capture is a
    /// separate matter and is not wired yet).
    /// </summary>
    private static void PlayWavFile(string path)
    {
        var player = new System.Media.SoundPlayer(path);
        player.Play();   // asynchronous: never blocks the dispatcher
    }

    /// <summary>Probe one sidecar on a worker thread and republish the diagnostics when it settles.</summary>
    private void StartSidecar(SidecarManager sidecar) => Task.Run(() =>
    {
        sidecar.Start();
        PublishDiagnostics();
    });

    /// <summary>
    /// Keep the sidecar that just served a translation resident, so the next sentence does
    /// not pay the model load again, and let its state stop claiming Warming once it can serve.
    ///
    /// Only the endpoint the response names is touched. Guessing from the mode would copy the
    /// router's rules into the UI, and warming both would pin a 2.95 GB checkpoint for an
    /// engine nobody used. Unmanaged engines (tm / demo / ollama) simply match nothing.
    /// </summary>
    private void WarmEndpointFor(string engine)
    {
        var manager = engine switch
        {
            "argos" => _argos,
            "madlad" => _madlad,
            _ => null,
        };
        if (manager is null) return;
        // Off the dispatcher: a warm-up is the model load itself, up to seconds on CPU.
        Task.Run(() =>
        {
            manager.Warm();
            PublishDiagnostics();
        });
    }

    /// <summary>Engine init result plus the live state of every sidecar endpoint.</summary>
    private void PublishDiagnostics()
    {
        string text;
        lock (_gate)
        {
            var sb = new StringBuilder(_engineLine);
            if (_argos is { } argos) AppendEndpoint(sb, "argos", argos);
            if (_madlad is { } madlad) AppendEndpoint(sb, "madlad", madlad);
            text = sb.ToString();
        }
        _window?.Dispatcher.Invoke(() => _vm?.SetDiagnostics(text));
    }

    /// <summary>
    /// One endpoint on the diagnostics line. A bare `Failed` would send the operator to restart a
    /// service that is answering perfectly well: what they need to know is that the port belongs
    /// to a *different* sidecar, so the knobs just configured never took effect (defect D73).
    /// `Unproven` is deliberately not shown -- the endpoint works, and a warning nobody can act on
    /// becomes noise that people learn to ignore.
    /// </summary>
    private static void AppendEndpoint(StringBuilder sb, string name, SidecarManager manager)
    {
        sb.Append(" · ").Append(name).Append('=').Append(manager.State);
        switch (manager.LastIdentityVerdict)
        {
            case Identity.Foreign:
                sb.Append("（端口被另一个 sidecar 占用，本次启动的参数未生效）");
                break;
            case Identity.Exited:
                sb.Append("（本程序启动的 sidecar 已退出，端口上是旧实例）");
                break;
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        // Reap sidecar child processes (kill only what this instance launched), then shut the Rust core down.
        _argos?.Stop();
        _madlad?.Stop();

        // Flush pending TM / glossary JSON writes in the Rust core. The distill
        // worker is not stopped here; its inbox closes with the process.
        EngineNative.Shutdown();
        base.OnExit(e);
    }
}
