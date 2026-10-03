// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using Wonslate.Audio;
using Wonslate.ViewModels;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// M4 voice panel tests. Models, pipeline, file dialog and player are all injected, so
/// the panel's state machine is exercised without a microphone, a speaker or a model.
/// </summary>
public class VoiceViewModelTests
{
    private static VoiceModelPaths Ready() =>
        new("vad.onnx", @"C:\asr", @"C:\tts", Array.Empty<string>());

    private static VoiceModelPaths Missing() =>
        new(null, @"C:\asr", @"C:\tts", new[] { "VAD (silero_vad.onnx)", "ASR (tokens.txt)" });

    /// <summary>Write a short real WAV so RunAsync's file read is exercised, not faked.</summary>
    private static string TempWav()
    {
        var path = Path.Combine(Path.GetTempPath(), $"wonslate-voice-test-{Guid.NewGuid():N}.wav");
        WavIo.Write(path, new float[16000], 16000);
        return path;
    }

    private sealed class Fakes
    {
        public string LastPlayed = "";
        public bool PlayCalled;
    }

    private sealed class StubSegmenter : ISpeechSegmenter
    {
        private readonly IReadOnlyList<AudioSegment> _segments;
        public StubSegmenter(params AudioSegment[] s) => _segments = s;
        public IReadOnlyList<AudioSegment> Segment(float[] samples, int sampleRate) => _segments;
        public void Dispose() { }
    }

    private sealed class StubRecognizer : ISpeechRecognizer
    {
        public string Transcribe(AudioSegment segment) => "你好";
        public void Dispose() { }
    }

    private sealed class StubSynth : ISpeechSynthesizer
    {
        public (float[] Samples, int SampleRate) Synthesize(string text, string lang)
            => (new float[24000], 24000);
        public void Dispose() { }
    }

    private sealed class StubTranslator : IVoiceTranslator
    {
        public VoiceTranslation Translate(string text, VoiceOptions options)
            => new(true, "Hello", "local", "demo", null, null);
    }

    private static VoicePipeline StubPipeline() => new(
        new StubSegmenter(new AudioSegment(new float[16000], 16000, 0)),
        new StubRecognizer(), new StubSynth(), new StubTranslator());

    private static VoiceOptions Opts() => new("zh", "en", "realtime", false);

    // ---- model presence ------------------------------------------------------

    [Fact]
    public void MissingModels_AreReportedAndBlockRunning()
    {
        var vm = new VoiceViewModel(locateModels: Missing, pipelineFactory: StubPipeline);

        Assert.False(vm.ModelsReady);
        Assert.False(vm.CanRun);
        Assert.Contains("缺少语音模型", vm.Status);
        Assert.Contains("fetch_voice_models.py", vm.Status);
    }

    [Fact]
    public async Task MissingModels_RunRefusesInsteadOfThrowing()
    {
        var vm = new VoiceViewModel(locateModels: Missing, pipelineFactory: StubPipeline);
        vm.SetFile(TempWav());

        await vm.RunAsync(Opts());

        Assert.False(vm.IsBusy);
        Assert.Contains("缺少语音模型", vm.Status);
    }

    // ---- file selection ------------------------------------------------------

    [Fact]
    public void SelectFile_UsesTheInjectedPicker()
    {
        var wav = TempWav();
        var vm = new VoiceViewModel(locateModels: Ready, pipelineFactory: StubPipeline, pickFile: () => wav);

        vm.SelectFile();

        Assert.Equal(wav, vm.FilePath);
        Assert.True(vm.CanRun);
        Assert.Contains(Path.GetFileName(wav), vm.Status);
    }

    [Fact]
    public void SelectFile_CancelledPicker_KeepsState()
    {
        var vm = new VoiceViewModel(locateModels: Ready, pipelineFactory: StubPipeline, pickFile: () => null);

        vm.SelectFile();

        Assert.Equal("", vm.FilePath);
        Assert.False(vm.CanRun);
    }

    // ---- running ------------------------------------------------------------

    [Fact]
    public async Task Run_PublishesTextSequenceAndTimings()
    {
        var vm = new VoiceViewModel(locateModels: Ready, pipelineFactory: StubPipeline);
        vm.SetFile(TempWav());

        await vm.RunAsync(Opts());

        Assert.Equal("你好", vm.RecognizedText);
        Assert.Equal("Hello", vm.TranslatedText);
        Assert.Contains("VAD", vm.TimingText);
        Assert.Contains("首段可播", vm.TimingText);
        Assert.Equal("", vm.Notes);
        Assert.True(vm.CanPlay);
        Assert.False(vm.IsBusy);
        Assert.Contains("完成", vm.Status);
    }

    [Fact]
    public async Task Run_FailureIsReportedNotThrown()
    {
        var vm = new VoiceViewModel(
            locateModels: Ready,
            pipelineFactory: () => throw new InvalidOperationException("sherpa runtime missing"));
        vm.SetFile(TempWav());

        await vm.RunAsync(Opts());

        Assert.False(vm.IsBusy);
        Assert.Contains("运行失败", vm.Status);
        Assert.Contains("sherpa runtime missing", vm.Status);
        Assert.False(vm.CanPlay);
    }

    [Fact]
    public async Task Run_UnreadableFile_IsReportedNotThrown()
    {
        var vm = new VoiceViewModel(locateModels: Ready, pipelineFactory: StubPipeline);
        vm.SetFile(Path.Combine(Path.GetTempPath(), "definitely-not-a-wav.wav"));

        await vm.RunAsync(Opts());

        Assert.Contains("运行失败", vm.Status);
        Assert.False(vm.IsBusy);
    }

    [Fact]
    public async Task Run_Cancelled_ReportsCancellation()
    {
        var vm = new VoiceViewModel(locateModels: Ready, pipelineFactory: StubPipeline);
        vm.SetFile(TempWav());
        using var cts = new CancellationTokenSource();
        cts.Cancel();

        await vm.RunAsync(Opts(), cts.Token);

        Assert.Contains("已取消", vm.Status);
        Assert.False(vm.IsBusy);
    }

    [Fact]
    public async Task Run_Degradations_AreSurfacedInNotes()
    {
        // Nothing recognized -> the pipeline reports it; the panel must show it rather
        // than presenting an empty result as success.
        var vm = new VoiceViewModel(
            locateModels: Ready,
            pipelineFactory: () => new VoicePipeline(
                new StubSegmenter(), new StubRecognizer(), new StubSynth(), new StubTranslator()));
        vm.SetFile(TempWav());

        await vm.RunAsync(Opts());

        Assert.Contains("没有检测到语音", vm.Notes);
        Assert.Contains("没有产出语音", vm.Status);
    }

    [Fact]
    public async Task Run_WithoutFile_DoesNotStart()
    {
        var vm = new VoiceViewModel(locateModels: Ready, pipelineFactory: StubPipeline);

        await vm.RunAsync(Opts());

        Assert.Contains("请先选择", vm.Status);
        Assert.False(vm.IsBusy);
    }

    // ---- playback -----------------------------------------------------------

    [Fact]
    public void PlayLast_WithoutAudio_SaysSo()
    {
        var vm = new VoiceViewModel(locateModels: Ready, pipelineFactory: StubPipeline);

        vm.PlayLast();

        Assert.Contains("还没有可播放", vm.Status);
    }

    [Fact]
    public async Task PlayLast_AfterARun_HandsATempWavToThePlayer()
    {
        var fakes = new Fakes();
        var vm = new VoiceViewModel(
            locateModels: Ready,
            pipelineFactory: StubPipeline,
            playWav: path => { fakes.PlayCalled = true; fakes.LastPlayed = path; });
        vm.SetFile(TempWav());
        await vm.RunAsync(Opts());

        vm.PlayLast();

        Assert.True(fakes.PlayCalled);
        Assert.True(File.Exists(fakes.LastPlayed));
        Assert.EndsWith(".wav", fakes.LastPlayed);
        Assert.Contains("正在播放", vm.Status);
    }

    [Fact]
    public void BusyState_IsExposedForTheUi()
    {
        var vm = new VoiceViewModel(locateModels: Ready, pipelineFactory: StubPipeline);
        Assert.True(vm.IsNotBusy);

        var raised = new List<string?>();
        vm.PropertyChanged += (_, e) => raised.Add(e.PropertyName);
        vm.SetFile(TempWav());

        Assert.Contains(nameof(VoiceViewModel.CanRun), raised);
    }
}
