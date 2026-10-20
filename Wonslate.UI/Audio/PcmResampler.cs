// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

namespace Wonslate.Audio;

/// <summary>
/// The 16 kHz mono alignment the voice models require (M4).
///
/// `SherpaVoiceStages.Segment` refuses any rate other than 16 kHz, and a capture device is free
/// to hand back 44.1 or 48 kHz, so a recorded buffer must be converted before it can enter the
/// pipeline. Linear interpolation is deliberate: the input is speech samples, and this is a
/// format conversion, not the quality-critical path (the recogniser downsamples/feature-extracts
/// internally anyway). What matters here is that the conversion is *explicit* -- a device rate
/// that silently reaches the VAD would throw deep inside sherpa with a message about Hz.
///
/// The rules mirror the repo's failure discipline: an already-16k buffer is returned untouched
/// (no needless copy), an empty buffer stays empty, and a non-positive rate is a loud refusal --
/// "rate 0 means skip conversion" would be the kind of silent default this repo keeps removing.
/// </summary>
public static class PcmResampler
{
    /// <summary>The rate every voice stage expects.</summary>
    public const int TargetRate = 16000;

    public static bool IsTarget(int sampleRate) => sampleRate == TargetRate;

    /// <summary>Convert mono float samples at <paramref name="sourceRate"/> to 16 kHz.</summary>
    public static float[] To16k(float[] samples, int sourceRate)
    {
        ArgumentNullException.ThrowIfNull(samples);
        if (sourceRate <= 0)
            throw new ArgumentOutOfRangeException(nameof(sourceRate), sourceRate,
                "sample rate must be positive; 0 is not a synonym for 'already 16 kHz'");
        if (samples.Length == 0) return Array.Empty<float>();
        if (sourceRate == TargetRate) return samples;

        int outputLength = (int)Math.Round(samples.Length * (double)TargetRate / sourceRate);
        if (outputLength <= 0) outputLength = 1;

        var output = new float[outputLength];
        double step = (double)sourceRate / TargetRate;   // source samples per output sample
        int last = samples.Length - 1;
        for (int i = 0; i < outputLength; i++)
        {
            double position = i * step;
            int left = (int)Math.Floor(position);
            if (left >= samples.Length) left = last;
            int right = Math.Min(left + 1, last);
            double fraction = position - left;
            if (fraction < 0) fraction = 0;
            output[i] = (float)(samples[left] * (1.0 - fraction) + samples[right] * fraction);
        }
        return output;
    }
}
