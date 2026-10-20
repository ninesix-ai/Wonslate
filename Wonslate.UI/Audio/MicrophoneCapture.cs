// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using NAudio.Wave;

namespace Wonslate.Audio;

/// <summary>The result of one capture: samples, the rate they arrived at, and why anything was lost.</summary>
/// <param name="DroppedSamples">
/// Samples refused because the recording hit <see cref="MicrophoneCapture.MaxSeconds"/>. Non-zero
/// means the caller heard less than was spoken, so the caller must surface it rather than present a
/// truncated recording as complete (the D80 "short body read as success" lesson, in audio).
/// </param>
public sealed record CaptureResult(float[] Samples, int SampleRate, int DroppedSamples, string? Note)
{
    public bool HasAudio => Samples.Length > 0;
    public double DurationSeconds => SampleRate > 0 ? Samples.Length / (double)SampleRate : 0;
}

/// <summary>
/// One microphone session: <see cref="Start"/> begins capturing, <see cref="Stop"/> ends it and
/// returns everything recorded. Injectable so the panel's recording state machine is testable
/// without a microphone (CI runners have none); the NAudio-backed implementation is
/// <see cref="MicrophoneCapture"/>.
/// </summary>
public interface IAudioCapture : IDisposable
{
    /// <summary>The rate this device delivers. Callers resample with <see cref="PcmResampler"/>.</summary>
    int SampleRate { get; }

    bool IsCapturing { get; }

    /// <summary>Begin capturing. Throws if already capturing, or when no device is available.</summary>
    void Start();

    /// <summary>End capturing and return what was recorded. Never throws for "nothing recorded".</summary>
    CaptureResult Stop();
}

/// <summary>
/// NAudio (WaveInEvent) implementation at 16 kHz mono 16-bit -- the format the sherpa models want,
/// so the common path needs no resampling. `PcmResampler` still guards the edge: if a device hands
/// back a different rate, the panel converts rather than letting the VAD throw deep inside sherpa.
///
/// Two safety rules, both learned from this repo's other "unbounded" bugs (D46): a recording has a
/// hard ceiling (<see cref="MaxSeconds"/>) so a forgotten stop cannot consume memory without limit,
/// and hitting it is *reported* in <see cref="CaptureResult.DroppedSamples"/> instead of silently
/// looking like a complete recording. Starting twice is a caller bug and throws; stopping when not
/// capturing returns an empty result with a note, never a crash.
/// </summary>
public sealed class MicrophoneCapture : IAudioCapture
{
    public const int Rate = 16000;

    private readonly object _gate = new();
    private readonly List<float> _samples = new();
    private readonly int _maxSeconds;
    private WaveInEvent? _device;
    private int _dropped;
    private bool _capturing;

    public MicrophoneCapture(int maxSeconds = 60)
    {
        if (maxSeconds <= 0)
            throw new ArgumentOutOfRangeException(nameof(maxSeconds), maxSeconds,
                "a recording needs a positive ceiling; 0 would mean 'unbounded'");
        _maxSeconds = maxSeconds;
    }

    public int SampleRate => Rate;

    public bool IsCapturing
    {
        get { lock (_gate) return _capturing; }
    }

    /// <summary>The ceiling in samples, exposed so tests can reason about it without a device.</summary>
    public int MaxSamples => _maxSeconds * Rate;

    public void Start()
    {
        lock (_gate)
        {
            if (_capturing)
                throw new InvalidOperationException("already capturing; stop the current recording first");
            _samples.Clear();
            _dropped = 0;
            var device = new WaveInEvent
            {
                WaveFormat = new WaveFormat(Rate, 16, 1),
                BufferMilliseconds = 50,
            };
            device.DataAvailable += OnData;
            try
            {
                device.StartRecording();          // throws when there is no capture device
            }
            catch
            {
                device.DataAvailable -= OnData;
                device.Dispose();
                throw;
            }
            _device = device;
            _capturing = true;
        }
    }

    public CaptureResult Stop()
    {
        WaveInEvent? device;
        float[] samples;
        int dropped;
        bool wasCapturing;
        lock (_gate)
        {
            wasCapturing = _capturing;
            device = _device;
            _device = null;
            _capturing = false;
            samples = _samples.ToArray();
            dropped = _dropped;
            _samples.Clear();
            _dropped = 0;
        }
        if (device is not null)
        {
            try { device.StopRecording(); } catch (ObjectDisposedException) { }
            device.DataAvailable -= OnData;
            device.Dispose();
        }
        if (!wasCapturing)
            return new CaptureResult(Array.Empty<float>(), Rate, 0, "没有正在进行的录音");
        string? note = dropped > 0
            ? $"已达到 {_maxSeconds}s 录音上限，后 {dropped} 个采样被丢弃"
            : null;
        return new CaptureResult(samples, Rate, dropped, note);
    }

    private void OnData(object? sender, WaveInEventArgs e)
    {
        lock (_gate)
        {
            if (!_capturing) return;
            int room = MaxSamples - _samples.Count;
            if (room <= 0)
            {
                _dropped += e.BytesRecorded / 2;
                return;
            }
            int usable = Math.Min(e.BytesRecorded, room * 2);
            for (int i = 0; i + 1 < usable; i += 2)
                _samples.Add(BitConverter.ToInt16(e.Buffer, i) / 32768.0f);
            if (usable < e.BytesRecorded)
                _dropped += (e.BytesRecorded - usable) / 2;
        }
    }

    public void Dispose()
    {
        WaveInEvent? device;
        lock (_gate)
        {
            device = _device;
            _device = null;
            _capturing = false;
            _samples.Clear();
        }
        if (device is not null)
        {
            try { device.StopRecording(); } catch (ObjectDisposedException) { }
            device.DataAvailable -= OnData;
            device.Dispose();
        }
    }
}
