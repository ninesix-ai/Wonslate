// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using Wonslate.Audio;
using Wonslate.ViewModels;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// M4 recording state machine. The microphone itself is injected, so every transition is exercised
/// without hardware: CI has no capture device, and a test that needs one is a test that never runs.
/// What is pinned here is exactly what the finished product must do: align to 16 kHz before the VAD,
/// refuse double-start, report an empty recording instead of running silence, and keep a truncation
/// note visible with the result.
/// </summary>
public class VoiceCaptureTests
{
    private static VoiceModelPaths Ready() =>
        new("vad.onnx", @"C:\asr", @"C:\tts", Array.Empty<string>());

    private static VoiceModelPaths Missing() =>
        new(null, @"C:\asr", @"C:\tts", new[] { "VAD (silero_vad.onnx)" });

    private static VoiceOptions Opts() => new("zh", "en", "realtime", false);

    private sealed class FakeCapture : IAudioCapture
    {
        public int SampleRate { get; init; } = 16000;
        public float[] Samples { get; init; } = Array.Empty<float>();
        public int Dropped { get; init; }
        public string? Note { get; init; }
        public bool ThrowOnStart { get; init; }
        public bool IsCapturing { get; private set; }
        public int StartCalls { get; private set; }
        public int StopCalls { get; private set; }

        public void Start()
        {
            StartCalls++;
            if (ThrowOnStart) throw new InvalidOperationException("no capture device");
            if (IsCapturing) throw new InvalidOperationException("already capturing");
            IsCapturing = true;
        }

        public CaptureResult Stop()
        {
            StopCalls++;
            bool was = IsCapturing;
            IsCapturing = false;
            return was
                ? new CaptureResult(Samples, SampleRate, Dropped, Note)
                : new CaptureResult(Array.Empty<float>(), SampleRate, 0, "没有正在进行的录音");
        }

        public void Dispose() { }
    }

    private sealed class RecordingSegmenter : ISpeechSegmenter
    {
        public int Calls { get; private set; }
        public int SeenRate { get; private set; } = -1;
        public int SeenLength { get; private set; } = -1;

        public IReadOnlyList<AudioSegment> Segment(float[] samples, int sampleRate)
        {
            Calls++;
            SeenRate = sampleRate;
            SeenLength = samples.Length;
            return samples.Length == 0
                ? Array.Empty<AudioSegment>()
                : new[] { new AudioSegment(samples, sampleRate, 0) };
        }

        public void Dispose() { }
    }

    private sealed class StubRecognizer : ISpeechRecognizer
    {
        public string Transcribe(AudioSegment segment) => "你好";
        public void Dispose() { }
    }

    private sealed class StubSynth : ISpeechSynthesizer
    {
        public (float[], int) Synthesize(string text, string lang) => (new float[2400], 24000);
        public void Dispose() { }
    }

    private sealed class StubTranslator : IVoiceTranslator
    {
        public VoiceTranslation Translate(string text, VoiceOptions options)
            => new(true, "Hello", "local", "demo", null, null);
    }

    private static VoicePipeline PipelineWith(RecordingSegmenter segmenter) => new(
        segmenter, new StubRecognizer(), new StubSynth(), new StubTranslator());

    private static VoiceViewModel Vm(FakeCapture capture, RecordingSegmenter? segmenter = null) =>
        new(locateModels: Ready,
            pipelineFactory: () => PipelineWith(segmenter ?? new RecordingSegmenter()),
            captureFactory: () => capture);

    [Fact]
    public async Task RecordedFortyEightK_ReachesThePipelineAsSixteenK()
    {
        var segmenter = new RecordingSegmenter();
        var capture = new FakeCapture { SampleRate = 48000, Samples = new float[48000] };
        var vm = Vm(capture, segmenter);

        vm.StartRecording();
        await vm.StopRecordingAndRunAsync(Opts());

        Assert.Equal(16000, segmenter.SeenRate);          // the VAD's hard requirement
        Assert.Equal(16000, segmenter.SeenLength);         // 48000 samples @48k -> 16000 @16k
        Assert.Equal(1, segmenter.Calls);
        Assert.Equal("Hello", vm.TranslatedText);
        Assert.False(vm.IsRecording);
        Assert.False(vm.IsBusy);
        Assert.Contains("完成", vm.Status);
    }

    [Fact]
    public async Task AnEmptyRecordingIsReportedAndThePipelineNeverRuns()
    {
        var segmenter = new RecordingSegmenter();
        var capture = new FakeCapture { Samples = Array.Empty<float>() };
        var vm = Vm(capture, segmenter);

        vm.StartRecording();
        await vm.StopRecordingAndRunAsync(Opts());

        Assert.Contains("没有录到声音", vm.Status);
        Assert.Equal(0, segmenter.Calls);
        Assert.Equal("", vm.TranslatedText);
        Assert.False(vm.IsRecording);
    }

    [Fact]
    public void StartingTwiceIsRefused_NotThrown()
    {
        var capture = new FakeCapture { Samples = new float[1600] };
        var vm = Vm(capture);

        vm.StartRecording();
        vm.StartRecording();

        Assert.True(vm.IsRecording);
        Assert.Contains("已经在录音", vm.Status);
        Assert.Equal(1, capture.StartCalls);              // the second click never reached the device
    }

    [Fact]
    public void ADeviceFailureIsReported_NotThrownAtTheDispatcher()
    {
        var capture = new FakeCapture { ThrowOnStart = true };
        var vm = Vm(capture);

        vm.StartRecording();

        Assert.False(vm.IsRecording);
        Assert.Contains("无法开始录音", vm.Status);
        Assert.Contains("no capture device", vm.Status);
    }

    [Fact]
    public async Task StoppingWithoutRecordingSaysSo()
    {
        var segmenter = new RecordingSegmenter();
        var vm = Vm(new FakeCapture(), segmenter);

        await vm.StopRecordingAndRunAsync(Opts());

        Assert.Contains("当前没有在录音", vm.Status);
        Assert.Equal(0, segmenter.Calls);
    }

    [Fact]
    public void MissingModels_BlockRecording()
    {
        var capture = new FakeCapture();
        var vm = new VoiceViewModel(locateModels: Missing, captureFactory: () => capture);

        vm.StartRecording();

        Assert.Contains("缺少语音模型", vm.Status);
        Assert.False(vm.IsRecording);
        Assert.Equal(0, capture.StartCalls);
    }

    [Fact]
    public async Task CanRecordAndCanStopTrackTheStateMachine()
    {
        var vm = Vm(new FakeCapture { Samples = new float[1600] });

        Assert.True(vm.CanRecord);
        Assert.False(vm.CanStopRecording);

        vm.StartRecording();
        Assert.False(vm.CanRecord);
        Assert.True(vm.CanStopRecording);

        await vm.StopRecordingAndRunAsync(Opts());
        Assert.True(vm.CanRecord);
        Assert.False(vm.CanStopRecording);
    }

    [Fact]
    public async Task ATruncatedRecordingKeepsItsNoteWithTheResult()
    {
        // The cap fired: the user must see that the tail was dropped, next to the translation.
        var capture = new FakeCapture
        {
            Samples = new float[16000],
            Dropped = 96000,
            Note = "已达到 60s 录音上限，后 96000 个采样被丢弃",
        };
        var vm = Vm(capture);

        vm.StartRecording();
        await vm.StopRecordingAndRunAsync(Opts());

        Assert.Contains("录音上限", vm.Notes);
        Assert.Contains("96000", vm.Notes);
    }

    [Fact]
    public async Task TheRecordingEntryPointDoesNotNeedAFile()
    {
        // M4 is about the microphone: the panel must run with FilePath empty, unlike RunAsync.
        var vm = Vm(new FakeCapture { Samples = new float[1600] });
        Assert.False(vm.CanRun);                          // no file selected

        vm.StartRecording();
        await vm.StopRecordingAndRunAsync(Opts());

        Assert.Equal("Hello", vm.TranslatedText);
    }
}
