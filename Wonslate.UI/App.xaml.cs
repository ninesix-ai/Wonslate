// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.Windows;
using System.Threading.Tasks;
using Wonslate.Interop;
using Wonslate.Sidecar;

namespace Wonslate;

public partial class App : Application
{
    // Phase 2 sidecar 引擎（argos 实时档 / madlad 冷门兜底）的进程与生命周期管理。
    // 未就绪/缺席不影响启动：Rust 路由会显式回落 demo，翻译功能不崩。
    private SidecarManager? _argos;
    private SidecarManager? _madlad;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        // 初始化 Rust 核心层（打开 TM DB、加载配置、启动蒸馏线程）
        EngineNative.Init();

        // 后台拉起/探测 sidecar，绝不阻塞 UI 启动；失败仅记状态，不抛。
        _argos = new SidecarManager(SidecarSpecFactory.Argos());
        _madlad = new SidecarManager(SidecarSpecFactory.Madlad());
        Task.Run(() => _argos.Start());
        Task.Run(() => _madlad.Start());
    }

    protected override void OnExit(ExitEventArgs e)
    {
        // 回收 sidecar 子进程（仅当确由本实例拉起时才 kill），再关 Rust 核心。
        _argos?.Stop();
        _madlad?.Stop();

        // 关闭 Rust 核心层（刷新 SQLite WAL，停止蒸馏线程）
        EngineNative.Shutdown();
        base.OnExit(e);
    }
}
