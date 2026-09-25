// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System.Windows;
using System.Runtime.CompilerServices;

[assembly: ThemeInfo(
    ResourceDictionaryLocation.None,
    ResourceDictionaryLocation.SourceAssembly
)]

// 允许 xUnit 测试项目访问 internal 类型（EngineNative / DTOs 等）
[assembly: InternalsVisibleTo("Wonslate.UI.Tests")]
