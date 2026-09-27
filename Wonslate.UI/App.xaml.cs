// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.Windows;
using System.Threading.Tasks;
using Wonslate.Interop;
using Wonslate.Sidecar;

namespace Wonslate;

public partial class App : Application
{
    // Process / lifecycle management for the Phase 2 sidecar engines (argos realtime slot / madlad rare fallback).
    // Not-ready / absent never blocks startup: the Rust router falls back to demo explicitly, translation keeps working.
    private SidecarManager? _argos;
    private SidecarManager? _madlad;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        // Initialize the Rust core (open the TM store, load config, start the distill thread)
        EngineNative.Init();

        // Bring up / probe sidecars in the background; never block UI startup; failures only set state, never throw.
        _argos = new SidecarManager(SidecarSpecFactory.Argos());
        _madlad = new SidecarManager(SidecarSpecFactory.Madlad());
        Task.Run(() => _argos.Start());
        Task.Run(() => _madlad.Start());
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
