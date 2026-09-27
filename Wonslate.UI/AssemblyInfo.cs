// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System.Windows;
using System.Runtime.CompilerServices;

[assembly: ThemeInfo(
    ResourceDictionaryLocation.None,
    ResourceDictionaryLocation.SourceAssembly
)]

// Let the xUnit test project see internal types (EngineNative / DTOs, etc.)
[assembly: InternalsVisibleTo("Wonslate.UI.Tests")]
