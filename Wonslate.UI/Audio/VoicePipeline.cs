// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using Wonslate.Interop;

namespace Wonslate.Audio;

/// <summary>Options for one voice translation, mirroring the text pipeline's request.</summary>
public sealed record VoiceOptions(string SourceLang, string TargetLang, string Mode, bool Privacy);

/// <summary>One speech segment produced by the VAD stage (16 kHz mono, -1..1).</summary>
public sealed record AudioSegment(float[] Samples, int SampleRate, double StartSeconds)
{
    public double DurationSeconds => Samples.Length / (double)SampleRate;
}

/// <summary>
/// Result of one translation, narrowed to what the pipeline needs. The engine's own
/// DTO stays inside the interop layer so this assembly's public surface does not
/// leak an internal type.
/// </summary>
public sealed record VoiceTranslation(
    bool Ok, string Text, string Source, string Engine, string? Error, string? Message);

/// <summary>Where each stage's time went, in milliseconds.</summary>
/// <param name="FirstAudioMs">
/// End of speech to the first byte of translated audio the caller could play. The MVP
/// is NOT streaming: TTS returns the whole utterance at once, so this equals
/// segment+ASR+translate+TTS for the first segment rather than a true first-token
/// figure. Recorded separately so a streaming implementation can improve it without
/// changing the meaning of the other fields.
/// </param>
public sealed record VoiceTimings(
    double SegmentMs, double AsrMs, double TranslateMs, double TtsMs,
    double FirstAudioMs, double TotalMs);

/// <summary>Everything one voice run produced, including why it degraded.</summary>
public sealed record VoiceResult(
    string RecognizedText, string TranslatedText, string TargetLang,
    float[] Samples, int SampleRate, VoiceTimings Timings, IReadOnlyList<string> Notes)
{
    public bool HasAudio => Samples.Length > 0;
}

// ---- Stage seams (injectable so the orchestration is testable without models) ----

/// <summary>Splits one utterance into speech segments (Silero VAD in production).</summary>
public interface ISpeechSegmenter : IDisposable
{
    IReadOnlyList<AudioSegment> Segment(float[] samples, int sampleRate);
}

/// <summary>Turns one speech segment into text (SenseVoice in production).</summary>
public interface ISpeechRecognizer : IDisposable
{
    string Transcribe(AudioSegment segment);
}

/// <summary>Turns text into speech samples (Kokoro in production).</summary>
public interface ISpeechSynthesizer : IDisposable
{
    (float[] Samples, int SampleRate) Synthesize(string text, string languageCode);
}

/// <summary>Translates one utterance through the engine (Rust FFI in production).</summary>
public interface IVoiceTranslator
{
    VoiceTranslation Translate(string text, VoiceOptions options);
}

/// <summary>
/// Production translator: the same `tt_translate_full` entry the text UI uses, so
/// routing, TM hits, privacy handling and the "never silently degrade" contract are
/// shared rather than reimplemented for voice.
/// </summary>
public sealed class EngineVoiceTranslator : IVoiceTranslator
{
    public VoiceTranslation Translate(string text, VoiceOptions options)
    {
        var request = System.Text.Json.JsonSerializer.Serialize(new
        {
            input = text,
            source_lang = options.SourceLang,
            target_lang = options.TargetLang,
            mode = options.Mode == "realtime" ? "realtime" : "full",
            privacy = options.Privacy,
            use_tm = true,
        });
        var dto = EngineNative.TranslateResponseDto.FromJson(EngineNative.TranslateFull(request));
        return new VoiceTranslation(
            dto.Ok, dto.Output ?? "", dto.Source, dto.Engine, dto.Error, dto.Message);
    }
}

/// <summary>
/// M3: the voice loop -- VAD segmentation, ASR, translation, TTS, in that order, with
/// per-stage timing. Stays synchronous and UI-free on purpose: the caller decides
/// which thread to run it on (the WPF layer wraps it in Task.Run) and every stage can
/// be replaced in tests.
///
/// Degradation is always reported in <see cref="VoiceResult.Notes"/>, never swallowed:
/// a run that hears nothing, fails to transcribe or fails to translate still returns
/// a result carrying the reason.
/// </summary>
public sealed class VoicePipeline
{
    private readonly ISpeechSegmenter _segmenter;
    private readonly ISpeechRecognizer _recognizer;
    private readonly ISpeechSynthesizer _synthesizer;
    private readonly IVoiceTranslator _translator;
    private readonly Func<double> _clockMs;

    public VoicePipeline(
        ISpeechSegmenter segmenter,
        ISpeechRecognizer recognizer,
        ISpeechSynthesizer synthesizer,
        IVoiceTranslator translator,
        Func<double>? clockMs = null)
    {
        _segmenter = segmenter;
        _recognizer = recognizer;
        _synthesizer = synthesizer;
        _translator = translator;
        _clockMs = clockMs ?? (() => Environment.TickCount64);
    }

    public VoiceResult Run(float[] samples, int sampleRate, VoiceOptions options)
    {
        var notes = new List<string>();
        var startedAt = _clockMs();

        var segSw = _clockMs();
        var segments = _segmenter.Segment(samples, sampleRate);
        var segmentMs = _clockMs() - segSw;

        if (segments.Count == 0)
        {
            notes.Add("没有检测到语音（VAD 未切出任何语音段）");
            return Empty(segmentMs, _clockMs() - startedAt, notes);
        }

        var recognized = new List<string>();
        var translated = new List<string>();
        var audio = new List<float>();
        double asrMs = 0, translateMs = 0, ttsMs = 0, firstAudioMs = 0;
        int outputRate = sampleRate;

        for (int i = 0; i < segments.Count; i++)
        {
            var seg = segments[i];

            var t = _clockMs();
            var text = _recognizer.Transcribe(seg);
            asrMs += _clockMs() - t;

            if (string.IsNullOrWhiteSpace(text))
            {
                notes.Add($"第 {i + 1} 段未识别出文字，已跳过");
                continue;
            }
            recognized.Add(text);

            t = _clockMs();
            var tr = _translator.Translate(text, options);
            translateMs += _clockMs() - t;

            if (!tr.Ok || string.IsNullOrWhiteSpace(tr.Text))
            {
                notes.Add($"第 {i + 1} 段翻译失败（{tr.Error ?? "NO_RESULT"}）：{tr.Message ?? "无详情"}");
                continue;
            }
            translated.Add(tr.Text);

            t = _clockMs();
            var (voice, rate) = _synthesizer.Synthesize(tr.Text, options.TargetLang);
            ttsMs += _clockMs() - t;

            if (voice.Length == 0)
            {
                notes.Add($"第 {i + 1} 段合成失败，该段无语音输出");
                continue;
            }
            outputRate = rate;
            if (audio.Count == 0)
            {
                // First playable audio: what a streaming implementation must beat.
                firstAudioMs = _clockMs() - startedAt;
            }
            audio.AddRange(voice);
        }

        if (translated.Count == 0)
        {
            notes.Add("没有任何一段产出译文");
        }

        return new VoiceResult(
            RecognizedText: string.Join(" ", recognized),
            TranslatedText: string.Join(" ", translated),
            TargetLang: options.TargetLang,
            Samples: audio.ToArray(),
            SampleRate: outputRate,
            Timings: new VoiceTimings(
                segmentMs, asrMs, translateMs, ttsMs, firstAudioMs, _clockMs() - startedAt),
            Notes: notes);
    }

    private static VoiceResult Empty(double segmentMs, double totalMs, List<string> notes) =>
        new("", "", "", Array.Empty<float>(), 0,
            new VoiceTimings(segmentMs, 0, 0, 0, 0, totalMs), notes);
}
