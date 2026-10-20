// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using NAudio.Wave;

namespace Wonslate.Audio;

/// <summary>
/// Plays TTS chunks as they arrive (M5). The seam is injected into the view model so the streaming
/// wiring is testable without an audio device; <see cref="StreamingAudioPlayer"/> is the NAudio one.
/// </summary>
public interface IStreamingAudioPlayer : IDisposable
{
    /// <summary>Queue one chunk and start playback on the first one. Best-effort: may throw on no device.</summary>
    void Push(float[] samples, int sampleRate);

    /// <summary>Wait until queued audio has been played (bounded), then stop. Never blocks forever.</summary>
    void CompleteAndWait(TimeSpan timeout);
}

/// <summary>
/// NAudio WaveOutEvent over a BufferedWaveProvider: the first chunk starts playback immediately and
/// later chunks are appended, so the listener does not wait for the whole utterance (Kokoro's first
/// chunk measured 1.86 s against 4.89 s for the full sentence).
///
/// Details that matter: the provider is created from the first chunk's rate (all chunks of one
/// synthesis share it); float samples are converted to 16-bit PCM because that is what the provider
/// takes; and <see cref="CompleteAndWait"/> polls the buffered duration with a timeout so a stuck
/// device cannot hang the run. A device-less host throws from Push, which the caller treats as
/// "playback unavailable" rather than letting it break synthesis.
/// </summary>
public sealed class StreamingAudioPlayer : IStreamingAudioPlayer
{
    private readonly WaveOutEvent _output = new();
    private readonly object _gate = new();
    private BufferedWaveProvider? _buffer;
    private bool _started;

    public void Push(float[] samples, int sampleRate)
    {
        if (samples.Length == 0) return;
        lock (_gate)
        {
            if (_buffer is null)
            {
                _buffer = new BufferedWaveProvider(new WaveFormat(sampleRate, 16, 1))
                {
                    DiscardOnBufferOverflow = true,
                    BufferDuration = TimeSpan.FromSeconds(30),
                };
            }
            if (!_started)
            {
                _output.Init(_buffer);
                _output.Play();
                _started = true;
            }
        }

        var bytes = new byte[samples.Length * 2];
        for (int i = 0; i < samples.Length; i++)
        {
            int value = (int)Math.Clamp(samples[i] * 32767f, -32768f, 32767f);
            bytes[2 * i] = (byte)(value & 0xFF);
            bytes[2 * i + 1] = (byte)((value >> 8) & 0xFF);
        }
        _buffer!.AddSamples(bytes, 0, bytes.Length);
    }

    public void CompleteAndWait(TimeSpan timeout)
    {
        var watch = System.Diagnostics.Stopwatch.StartNew();
        lock (_gate)
        {
            if (_buffer is null || !_started) return;
            while (watch.Elapsed < timeout)
            {
                if (_buffer.BufferedDuration <= TimeSpan.FromMilliseconds(60)) break;
                Thread.Sleep(30);
            }
        }
        try { _output.Stop(); } catch (Exception) { /* a dead device is not a synthesis failure */ }
    }

    public void Dispose()
    {
        try { _output.Stop(); } catch (Exception) { }
        _output.Dispose();
    }
}
