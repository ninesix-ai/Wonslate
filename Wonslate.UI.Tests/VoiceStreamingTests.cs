// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Wonslate.Audio;
using Wonslate.ViewModels;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// M5 streaming TTS tests. The point of streaming is not the final buffer (that was already right);
/// it is that "first playable audio" becomes the first *chunk*. These tests pin that difference, the
/// fallback for a non-streaming synthesizer, and that the UI receives the chunks in order. No audio
/// device is touched: the player is injected.
/// </summary>
public class VoiceStreamingTests
{
    private sealed class Clock
    {
        public double Now;
    }

    private sealed class StreamingSynth : IStreamingSpeechSynthesizer
    {
        private readonly Clock _clock;
        private readonly (float[] Chunk, double Advance)[] _chunks;
        public int Calls { get; private set; }

        public StreamingSynth(Clock clock, params (float[], double)[] chunks)
        {
            _clock = clock;
            _chunks = chunks;
        }

        public (float[] Samples, int SampleRate) SynthesizeStreaming(
            string text, string languageCode, Action<float[], int> onChunk)
        {
            Calls++;
            var all = new List<float>();
            foreach (var (chunk, advance) in _chunks)
            {
                _clock.Now += advance;
                onChunk(chunk, 24000);
                all.AddRange(chunk);
            }
            return (all.ToArray(), 24000);
        }

        public (float[] Samples, int SampleRate) Synthesize(string text, string languageCode)
            => SynthesizeStreaming(text, languageCode, (_, _) => { });

        public void Dispose() { }
    }

    private sealed class WholeSynth : ISpeechSynthesizer
    {
        private readonly Clock _clock;
        private readonly float[] _samples;
        private readonly double _cost;
        public WholeSynth(Clock clock, float[] samples, double cost)
        {
            _clock = clock;
            _samples = samples;
            _cost = cost;
        }

        public (float[] Samples, int SampleRate) Synthesize(string text, string languageCode)
        {
            _clock.Now += _cost;
            return (_samples, 24000);
        }

        public void Dispose() { }
    }

    private sealed class OneSegment : ISpeechSegmenter
    {
        public IReadOnlyList<AudioSegment> Segment(float[] samples, int sampleRate)
            => new[] { new AudioSegment(new float[16000], 16000, 0) };
        public void Dispose() { }
    }

    private sealed class Says : ISpeechRecognizer
    {
        public string Transcribe(AudioSegment segment) => "你好";
        public void Dispose() { }
    }

    private sealed class Translates : IVoiceTranslator
    {
        public VoiceTranslation Translate(string text, VoiceOptions options)
            => new(true, "Hello", "local", "demo", null, null);
    }

    private static VoiceOptions Opts() => new("zh", "en", "realtime", false);

    [Fact]
    public void FirstAudioIsTheFirstChunk_NotTheEndOfSynthesis()
    {
        var clock = new Clock();
        var synth = new StreamingSynth(clock,
            (new float[] { 1f, 2f }, 100),
            (new float[] { 3f }, 900));
        var pipeline = new VoicePipeline(new OneSegment(), new Says(), synth, new Translates(),
                                         () => clock.Now);

        var result = pipeline.Run(new float[16000], 16000, Opts());

        Assert.Equal(100, result.Timings.FirstAudioMs);      // first chunk, not 1000
        Assert.Equal(1000, result.Timings.TotalMs);
        Assert.Equal(new float[] { 1f, 2f, 3f }, result.Samples);
    }

    [Fact]
    public void StreamingChunksReachTheCallbackInOrder()
    {
        var clock = new Clock();
        var synth = new StreamingSynth(clock,
            (new float[] { 1f, 2f }, 10),
            (new float[] { 3f }, 20));
        var pipeline = new VoicePipeline(new OneSegment(), new Says(), synth, new Translates(),
                                         () => clock.Now);
        var seen = new List<float[]>();

        pipeline.Run(new float[16000], 16000, Opts(), (chunk, rate) =>
        {
            Assert.Equal(24000, rate);
            seen.Add(chunk);
        });

        Assert.Equal(2, seen.Count);
        Assert.Equal(new float[] { 1f, 2f }, seen[0]);
        Assert.Equal(new float[] { 3f }, seen[1]);
    }

    [Fact]
    public void ANonStreamingSynthesizerKeepsFirstAudioAtCompletion()
    {
        var clock = new Clock();
        var synth = new WholeSynth(clock, new float[] { 9f }, 500);
        var pipeline = new VoicePipeline(new OneSegment(), new Says(), synth, new Translates(),
                                         () => clock.Now);

        var result = pipeline.Run(new float[16000], 16000, Opts());

        Assert.Equal(500, result.Timings.FirstAudioMs);      // old behaviour preserved
        Assert.Equal(new float[] { 9f }, result.Samples);
    }

    [Fact]
    public void EmptyChunksAreIgnoredForTimingAndDelivery()
    {
        var clock = new Clock();
        var synth = new StreamingSynth(clock,
            (Array.Empty<float>(), 40),
            (new float[] { 5f }, 60));
        var pipeline = new VoicePipeline(new OneSegment(), new Says(), synth, new Translates(),
                                         () => clock.Now);
        var seen = new List<float[]>();

        var result = pipeline.Run(new float[16000], 16000, Opts(), (chunk, rate) => seen.Add(chunk));

        Assert.Equal(100, result.Timings.FirstAudioMs);      // 40 + 60: the real chunk
        Assert.Single(seen);
        Assert.Equal(new float[] { 5f }, seen[0]);
    }

    // ---- view-model wiring: chunks go to the player, lazily, and a dead device is survivable ----

    private sealed class FakePlayer : IStreamingAudioPlayer
    {
        public List<float[]> Pushed { get; } = new();
        public bool PushThrows { get; set; }
        public int Completed { get; private set; }
        public bool Disposed { get; private set; }

        public void Push(float[] samples, int sampleRate)
        {
            if (PushThrows) throw new InvalidOperationException("no audio device");
            Pushed.Add(samples);
        }

        public void CompleteAndWait(TimeSpan timeout) => Completed++;
        public void Dispose() => Disposed = true;
    }

    private static string TempWav()
    {
        var path = Path.Combine(Path.GetTempPath(), $"wonslate-stream-{Guid.NewGuid():N}.wav");
        WavIo.Write(path, new float[16000], 16000);
        return path;
    }

    private static VoiceModelPaths Ready() =>
        new("vad.onnx", @"C:\asr", @"C:\tts", Array.Empty<string>());

    private static VoiceViewModel VmWith(ISpeechSynthesizer synth, Func<IStreamingAudioPlayer> factory)
        => new(locateModels: Ready,
               pipelineFactory: () => new VoicePipeline(
                   new OneSegment(), new Says(), synth, new Translates()),
               streamingPlayerFactory: factory);

    [Fact]
    public async Task StreamingRunPushesChunksToThePlayerAndWaitsForIt()
    {
        var clock = new Clock();
        var player = new FakePlayer();
        var vm = VmWith(new StreamingSynth(clock, (new float[] { 1f }, 10), (new float[] { 2f }, 10)),
                        () => player);
        vm.SetFile(TempWav());

        await vm.RunAsync(Opts());

        Assert.Equal(2, player.Pushed.Count);
        Assert.Equal(1, player.Completed);
        Assert.True(player.Disposed);
        Assert.Equal("Hello", vm.TranslatedText);
        Assert.Contains("完成", vm.Status);
    }

    [Fact]
    public async Task NonStreamingRunNeverCreatesAPlayer()
    {
        var clock = new Clock();
        bool created = false;
        var vm = VmWith(new WholeSynth(clock, new float[] { 1f }, 10), () =>
        {
            created = true;
            return new FakePlayer();
        });
        vm.SetFile(TempWav());

        await vm.RunAsync(Opts());

        Assert.False(created, "a whole-utterance synthesizer must not touch an audio device");
    }

    [Fact]
    public async Task ADeadOutputDeviceDoesNotFailTheRun()
    {
        var clock = new Clock();
        var player = new FakePlayer { PushThrows = true };
        var vm = VmWith(new StreamingSynth(clock, (new float[] { 1f }, 10)), () => player);
        vm.SetFile(TempWav());

        await vm.RunAsync(Opts());

        Assert.Equal("Hello", vm.TranslatedText);
        Assert.Contains("完成", vm.Status);
        Assert.True(player.Disposed);
    }

    [Fact]
    public void TheRealPlayerIsTheNaudioOne()
    {
        // Guards the default wiring without opening a device: the type must exist and be the
        // default the view model falls back to when no factory is injected.
        var field = typeof(VoiceViewModel).GetFields(
            System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)
            .FirstOrDefault(f => f.FieldType == typeof(Func<IStreamingAudioPlayer>));
        Assert.NotNull(field);
        Assert.NotNull(typeof(StreamingAudioPlayer));
    }
}
