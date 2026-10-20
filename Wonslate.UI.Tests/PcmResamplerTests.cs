// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System;
using Wonslate.Audio;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// 16 kHz alignment (M4). These are the pure cases: a capture device is free to deliver 44.1/48 kHz
/// and `SherpaVoiceStages.Segment` throws on anything but 16 kHz, so the conversion has to be exact
/// and loud about bad input. No device is touched here.
/// </summary>
public class PcmResamplerTests
{
    [Fact]
    public void AlreadySixteenK_IsReturnedUntouched()
    {
        var input = new float[] { 0.1f, -0.2f, 0.3f };
        var output = PcmResampler.To16k(input, 16000);
        Assert.Same(input, output);          // no needless copy, and no second source of truth
    }

    [Fact]
    public void FortyEightK_IsDecimatedToTheExpectedLength()
    {
        var input = new float[48000];
        var output = PcmResampler.To16k(input, 48000);
        Assert.Equal(16000, output.Length);
    }

    [Fact]
    public void EightK_IsUpsampledToTheExpectedLength()
    {
        var input = new float[8000];
        var output = PcmResampler.To16k(input, 8000);
        Assert.Equal(16000, output.Length);
    }

    [Fact]
    public void AConstantSignalStaysConstant_NotSilentlyZeroFilled()
    {
        // The classic "resampler that returns an empty/zero buffer" would pass a length check and
        // then make the VAD hear silence. Every sample must carry the input's value.
        var input = new float[4800];
        for (int i = 0; i < input.Length; i++) input[i] = 0.5f;

        var output = PcmResampler.To16k(input, 48000);

        Assert.Equal(1600, output.Length);
        foreach (var s in output) Assert.InRange(s, 0.499f, 0.501f);
    }

    [Fact]
    public void EmptyStaysEmpty()
    {
        Assert.Empty(PcmResampler.To16k(Array.Empty<float>(), 48000));
    }

    [Theory]
    [InlineData(0)]
    [InlineData(-1)]
    [InlineData(-44100)]
    public void ANonPositiveRateIsARefusal_NotAQuietNoOp(int rate)
    {
        // "0 means skip conversion" is the kind of silent default this repo keeps removing.
        Assert.Throws<ArgumentOutOfRangeException>(() => PcmResampler.To16k(new float[10], rate));
    }

    [Fact]
    public void ANullBufferIsARefusal()
    {
        Assert.Throws<ArgumentNullException>(() => PcmResampler.To16k(null!, 16000));
    }

    [Fact]
    public void IsTargetOnlyAcceptsSixteenK()
    {
        Assert.True(PcmResampler.IsTarget(16000));
        Assert.False(PcmResampler.IsTarget(48000));
        Assert.False(PcmResampler.IsTarget(0));
    }
}
