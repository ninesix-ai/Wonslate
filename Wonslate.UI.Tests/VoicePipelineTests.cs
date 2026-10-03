// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Wonslate.Audio;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// M3 orchestration tests. Every stage is a fake, so these run without models and
/// without a microphone and still pin the contract that matters: stage order, timing
/// accounting, and "degradation is always reported".
/// </summary>
public class VoicePipelineTests
{
    private sealed class FakeSegmenter : ISpeechSegmenter
    {
        private readonly IReadOnlyList<AudioSegment> _segments;
        public int Calls { get; private set; }
        public FakeSegmenter(params AudioSegment[] segments) => _segments = segments;
        public IReadOnlyList<AudioSegment> Segment(float[] samples, int sampleRate)
        {
            Calls++;
            return _segments;
        }
        public void Dispose() { }
    }

    private sealed class FakeRecognizer : ISpeechRecognizer
    {
        private readonly Func<AudioSegment, string> _fn;
        public List<int> SeenRates { get; } = new();
        public FakeRecognizer(Func<AudioSegment, string> fn) => _fn = fn;
        public string Transcribe(AudioSegment segment)
        {
            SeenRates.Add(segment.SampleRate);
            return _fn(segment);
        }
        public void Dispose() { }
    }

    private sealed class FakeSynthesizer : ISpeechSynthesizer
    {
        private readonly Func<string, (float[], int)> _fn;
        public List<string> Spoken { get; } = new();
        public FakeSynthesizer(Func<string, (float[], int)> fn) => _fn = fn;
        public (float[] Samples, int SampleRate) Synthesize(string text, string languageCode)
        {
            Spoken.Add($"{languageCode}:{text}");
            return _fn(text);
        }
        public void Dispose() { }
    }

    private sealed class FakeTranslator : IVoiceTranslator
    {
        private readonly Func<string, VoiceTranslation> _fn;
        public List<VoiceOptions> Options { get; } = new();
        public FakeTranslator(Func<string, VoiceTranslation> fn) => _fn = fn;
        public VoiceTranslation Translate(string text, VoiceOptions options)
        {
            Options.Add(options);
            return _fn(text);
        }
    }

    private static AudioSegment Seg(double start = 0) =>
        new(new float[16000], 16000, start);

    private static VoiceOptions Opts(bool privacy = false) =>
        new("zh", "en", "realtime", privacy);

    private static (float[] Samples, int Rate) Voice(int n = 24000) =>
        (Enumerable.Repeat(0.5f, n).ToArray(), 24000);

    [Fact]
    public void Run_BuildsTextAndAudioInStageOrder()
    {
        var seg = new FakeSegmenter(Seg());
        var rec = new FakeRecognizer(_ => "你好世界");
        var tts = new FakeSynthesizer(_ => Voice());
        var tr = new FakeTranslator(t => new VoiceTranslation(true, "Hello world", "local", "demo", null, null));

        var result = new VoicePipeline(seg, rec, tts, tr).Run(new float[32000], 16000, Opts());

        Assert.Equal("你好世界", result.RecognizedText);
        Assert.Equal("Hello world", result.TranslatedText);
        Assert.Equal("en", result.TargetLang);
        Assert.True(result.HasAudio);
        Assert.Equal(24000, result.SampleRate);
        Assert.Empty(result.Notes);
        // The synthesizer must be asked for the TARGET language, not the source.
        Assert.Equal("en:Hello world", Assert.Single(tts.Spoken));
    }

    [Fact]
    public void Run_NoSpeech_ReportsItInsteadOfReturningSilence()
    {
        var seg = new FakeSegmenter();   // VAD finds nothing
        var rec = new FakeRecognizer(_ => "unused");
        var tts = new FakeSynthesizer(_ => Voice());

        var result = new VoicePipeline(seg, rec, tts, new FakeTranslator(_ => throw new Exception()))
            .Run(new float[16000], 16000, Opts());

        Assert.False(result.HasAudio);
        Assert.Equal("", result.RecognizedText);
        Assert.Contains(result.Notes, n => n.Contains("没有检测到语音"));
    }

    [Fact]
    public void Run_UnrecognizedSegment_IsSkippedAndNoted()
    {
        var rec = new FakeRecognizer(_ => "   ");
        var tts = new FakeSynthesizer(_ => Voice());

        var result = new VoicePipeline(new FakeSegmenter(Seg()), rec, tts,
                new FakeTranslator(_ => throw new Exception("must not be called")))
            .Run(new float[16000], 16000, Opts());

        Assert.False(result.HasAudio);
        Assert.Contains(result.Notes, n => n.Contains("未识别出文字"));
    }

    [Fact]
    public void Run_TranslationFailure_SurfacesEngineErrorAndKeepsGoing()
    {
        var rec = new FakeRecognizer(_ => "你好");
        var tts = new FakeSynthesizer(_ => Voice());
        var tr = new FakeTranslator(_ => new VoiceTranslation(
            false, "", "fallback", "unknown", "NO_RESULT", "no engine could translate zh->en"));

        var result = new VoicePipeline(new FakeSegmenter(Seg()), rec, tts, tr)
            .Run(new float[16000], 16000, Opts());

        Assert.False(result.HasAudio);
        Assert.Contains(result.Notes, n => n.Contains("NO_RESULT") && n.Contains("no engine"));
        Assert.Contains(result.Notes, n => n.Contains("没有任何一段产出译文"));
    }

    [Fact]
    public void Run_SynthesisFailure_IsNoted()
    {
        var rec = new FakeRecognizer(_ => "你好");
        var tts = new FakeSynthesizer(_ => (Array.Empty<float>(), 0));
        var tr = new FakeTranslator(_ => new VoiceTranslation(true, "Hello", "local", "demo", null, null));

        var result = new VoicePipeline(new FakeSegmenter(Seg()), rec, tts, tr)
            .Run(new float[16000], 16000, Opts());

        Assert.False(result.HasAudio);
        Assert.Contains(result.Notes, n => n.Contains("合成失败"));
    }

    [Fact]
    public void Run_MultipleSegments_ConcatenateAudioAndText()
    {
        var i = 0;
        var rec = new FakeRecognizer(_ => $"句{++i}");
        var tts = new FakeSynthesizer(text => (Enumerable.Repeat(0.1f, text.Length * 100).ToArray(), 24000));
        var tr = new FakeTranslator(t => new VoiceTranslation(true, t.ToUpperInvariant(), "local", "demo", null, null));

        var result = new VoicePipeline(new FakeSegmenter(Seg(0), Seg(1.5)), rec, tts, tr)
            .Run(new float[64000], 16000, Opts());

        Assert.Equal("句1 句2", result.RecognizedText);
        Assert.Equal("句1 句2", result.TranslatedText);
        Assert.Equal(200 + 200, result.Samples.Length);   // two 2-character utterances at 100 samples/char
    }

    [Fact]
    public void Run_PrivacyFlag_IsPassedThroughToTheTranslator()
    {
        var tr = new FakeTranslator(_ => new VoiceTranslation(true, "x", "fallback", "demo", null, null));

        new VoicePipeline(new FakeSegmenter(Seg()), new FakeRecognizer(_ => "你好"),
                new FakeSynthesizer(_ => Voice()), tr)
            .Run(new float[16000], 16000, Opts(privacy: true));

        var seen = Assert.Single(tr.Options);
        Assert.True(seen.Privacy, "privacy must reach the engine so it can refuse to escalate");
        Assert.Equal("zh", seen.SourceLang);
        Assert.Equal("en", seen.TargetLang);
    }

    [Fact]
    public void Run_RecordsTimingsIncludingFirstAudio()
    {
        // A clock that advances 10 ms per read makes every stage's cost deterministic.
        double now = 0;
        double Clock() => now += 10;

        var pipeline = new VoicePipeline(
            new FakeSegmenter(Seg(0), Seg(1)),
            new FakeRecognizer(_ => "你好"),
            new FakeSynthesizer(_ => Voice(2400)),
            new FakeTranslator(_ => new VoiceTranslation(true, "Hello", "local", "demo", null, null)),
            Clock);

        var result = pipeline.Run(new float[32000], 16000, Opts());

        Assert.Equal(10, result.Timings.SegmentMs);
        Assert.Equal(20, result.Timings.AsrMs);          // two segments
        Assert.Equal(20, result.Timings.TranslateMs);
        Assert.Equal(20, result.Timings.TtsMs);
        Assert.True(result.Timings.FirstAudioMs > 0, "first playable audio must be timed");
        Assert.True(result.Timings.TotalMs >= result.Timings.FirstAudioMs);
    }

    [Fact]
    public void Run_ReportsTheSampleRateTheSynthesizerReturned()
    {
        var result = new VoicePipeline(new FakeSegmenter(Seg()), new FakeRecognizer(_ => "你好"),
                new FakeSynthesizer(_ => (new float[100], 48000)),
                new FakeTranslator(_ => new VoiceTranslation(true, "hi", "local", "demo", null, null)))
            .Run(new float[16000], 16000, Opts());

        Assert.Equal(48000, result.SampleRate);
    }

    [Fact]
    public void Run_FeedsTheVadAtTheRequestedRate()
    {
        var rec = new FakeRecognizer(_ => "x");
        new VoicePipeline(new FakeSegmenter(Seg()), rec, new FakeSynthesizer(_ => Voice()),
                new FakeTranslator(_ => new VoiceTranslation(true, "y", "local", "demo", null, null)))
            .Run(new float[16000], 16000, Opts());

        Assert.Equal(16000, Assert.Single(rec.SeenRates));
    }
}

/// <summary>
/// Model-path resolution: the rule every entry point shares, so the GUI cannot drift
/// from the scripts (docs/06 §7 pitfall 9 was exactly a hardcoded model path).
/// </summary>
public class VoiceModelLocatorTests
{
    private static Func<string, string?> Env(params (string Key, string Value)[] pairs) =>
        key =>
        {
            foreach (var (k, v) in pairs)
                if (k == key) return v;
            return null;
        };

    [Fact]
    public void Resolve_DefaultsToTheDataDirModelsFolder_OnWindows()
    {
        var paths = VoiceModelLocator.Resolve(Env(("LOCALAPPDATA", @"C:\Users\x\AppData\Local")));

        Assert.StartsWith(@"C:\Users\x\AppData\Local\Wonslate\models", paths.AsrDir);
        Assert.StartsWith(@"C:\Users\x\AppData\Local\Wonslate\models", paths.TtsDir);
    }

    [Fact]
    public void Resolve_DataDirOverride_WinsOverTheOsDefault()
    {
        var paths = VoiceModelLocator.Resolve(
            Env(("LT_DATA_DIR", @"D:\scratch"), ("LOCALAPPDATA", @"C:\Users\x\AppData\Local")));

        Assert.StartsWith(@"D:\scratch\models", paths.AsrDir);
    }

    [Fact]
    public void Resolve_LegacyLtPrefixWinsOverTheBrandSpelling()
    {
        var root = Path.Combine(Path.GetTempPath(), "wonslate-voice-root");
        var paths = VoiceModelLocator.Resolve(Env(
            ("LT_VOICE_MODEL_DIR", root),
            ("WONSLATE_VOICE_MODEL_DIR", Path.Combine(Path.GetTempPath(), "other"))));

        Assert.Equal(Path.Combine(root, "sense-voice"), paths.AsrDir);
    }

    [Fact]
    public void Resolve_AsrModelDirOverride_MatchesTheDemoConvention()
    {
        var paths = VoiceModelLocator.Resolve(Env(("ASR_MODEL_DIR", @"D:\Programs\local-models\sense-voice")));

        Assert.Equal(@"D:\Programs\local-models\sense-voice", paths.AsrDir);
    }

    [Fact]
    public void Resolve_ReportsEveryMissingComponent()
    {
        var paths = VoiceModelLocator.Resolve(Env(("LT_DATA_DIR", @"D:\definitely-not-here")));

        Assert.False(paths.IsComplete);
        Assert.Contains(paths.Missing, m => m.StartsWith("VAD"));
        Assert.Contains(paths.Missing, m => m.StartsWith("ASR"));
        Assert.Contains(paths.Missing, m => m.StartsWith("TTS"));
    }
}
