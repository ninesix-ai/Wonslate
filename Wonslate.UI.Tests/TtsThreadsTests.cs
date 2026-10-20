// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System;
using System.IO;
using Wonslate.Audio;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// TTS thread count (D88). The numbers come from a real warm-run scan on 8C/16T
/// (2: 1823/4699 ms, 4: 1669/4257, 8: 1619/4179, 12: 1672/4203, 16: 1882/4689 for first chunk /
/// whole sentence): one thread per physical core, capped at 8, because 12 and 16 regress.
/// The second test exists so "the helper is right" cannot coexist with "the helper is not used".
/// </summary>
public class TtsThreadsTests
{
    [Theory]
    [InlineData(1, 1)]
    [InlineData(2, 1)]
    [InlineData(4, 2)]
    [InlineData(8, 4)]
    [InlineData(16, 8)]
    [InlineData(24, 8)]
    [InlineData(32, 8)]
    public void ThreadsFollowPhysicalCoresWithAMeasuredCap(int processorCount, int expected)
    {
        Assert.Equal(expected, SherpaVoiceStages.TtsThreads(processorCount));
    }

    [Fact]
    public void TheTtsConfigUsesTheHelperWhileAsrKeepsItsOwnThreads()
    {
        string root = Path.GetFullPath(Path.Combine(
            AppContext.BaseDirectory, "..", "..", "..", ".."));
        string source = File.ReadAllText(
            Path.Combine(root, "Wonslate.UI", "Audio", "SherpaVoiceStages.cs"));
        Assert.Contains("ttsConfig.Model.NumThreads = TtsThreads(Environment.ProcessorCount);",
                        source);
        Assert.Contains("asrConfig.ModelConfig.NumThreads = NumThreads;", source);
    }
}
