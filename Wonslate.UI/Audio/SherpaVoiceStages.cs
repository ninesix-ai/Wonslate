// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.IO;
using SherpaOnnx;

namespace Wonslate.Audio;

/// <summary>
/// Production stages backed by sherpa-onnx: Silero VAD, SenseVoice ASR and Kokoro TTS.
///
/// Model layout and file names follow docs/06 §7: the int8 artifacts
/// (model.int8.onnx) are what these packages ship, all Kokoro side files are
/// mandatory, and 1.13.8's GenerateWithConfig has no overload without a callback.
/// Native objects are IDisposable and hold real memory, so the pipeline's caller owns
/// their lifetime; the app creates them once and disposes them at shutdown.
/// </summary>
public sealed class SherpaVoiceStages :
    ISpeechSegmenter, ISpeechRecognizer, ISpeechSynthesizer
{
    private const int SampleRate = 16000;
    private const int NumThreads = 2;

    /// <summary>Kokoro Mandarin voices are indexed by integer sid; 52 handles mixed CJK/Latin best.</summary>
    private const int KokoroZhSid = 52;
    private const int KokoroEnSid = 50;

    private readonly VoiceActivityDetector _vad;
    private readonly OfflineRecognizer _asr;
    private readonly OfflineTts _tts;
    private readonly float[] _vadBuffer = new float[512];

    public SherpaVoiceStages(VoiceModelPaths paths)
    {
        if (!paths.IsComplete)
            throw new InvalidOperationException(
                "voice models are incomplete: " + string.Join(", ", paths.Missing));

        var vadConfig = new VadModelConfig();
        vadConfig.SileroVad = new SileroVadModelConfig { Model = paths.VadModel! };
        vadConfig.SampleRate = SampleRate;
        _vad = new VoiceActivityDetector(vadConfig, 16.0f);

        var asrConfig = new OfflineRecognizerConfig();
        asrConfig.FeatConfig.SampleRate = SampleRate;
        asrConfig.FeatConfig.FeatureDim = 80;
        asrConfig.ModelConfig.SenseVoice = new OfflineSenseVoiceModelConfig
        {
            Model = Path.Combine(paths.AsrDir, "model.int8.onnx"),
            Language = "auto",
            UseInverseTextNormalization = 1,   // numbers and punctuation come back normalized
        };
        asrConfig.ModelConfig.Tokens = Path.Combine(paths.AsrDir, "tokens.txt");
        asrConfig.ModelConfig.NumThreads = NumThreads;
        asrConfig.ModelConfig.Provider = "cpu";
        _asr = new OfflineRecognizer(asrConfig);

        var ttsConfig = new OfflineTtsConfig();
        ttsConfig.Model.Kokoro.Model = Path.Combine(paths.TtsDir, "model.int8.onnx");
        ttsConfig.Model.Kokoro.Voices = Path.Combine(paths.TtsDir, "voices.bin");
        ttsConfig.Model.Kokoro.Tokens = Path.Combine(paths.TtsDir, "tokens.txt");
        ttsConfig.Model.Kokoro.DataDir = Path.Combine(paths.TtsDir, "espeak-ng-data");
        ttsConfig.Model.Kokoro.Lexicon =
            Path.Combine(paths.TtsDir, "lexicon-us-en.txt") + "," +
            Path.Combine(paths.TtsDir, "lexicon-zh.txt");
        ttsConfig.Model.NumThreads = NumThreads;
        ttsConfig.Model.Provider = "cpu";
        _tts = new OfflineTts(ttsConfig);
    }

    /// <summary>Feed the whole utterance through the VAD and drain the speech segments it finds.</summary>
    public IReadOnlyList<AudioSegment> Segment(float[] samples, int sampleRate)
    {
        if (sampleRate != SampleRate)
            throw new ArgumentException(
                $"VAD expects {SampleRate} Hz mono input, got {sampleRate} Hz");

        var segments = new List<AudioSegment>();
        _vad.Reset();
        for (int offset = 0; offset < samples.Length; offset += _vadBuffer.Length)
        {
            int n = Math.Min(_vadBuffer.Length, samples.Length - offset);
            Array.Copy(samples, offset, _vadBuffer, 0, n);
            _vad.AcceptWaveform(n == _vadBuffer.Length ? _vadBuffer : _vadBuffer[..n]);
        }
        _vad.Flush();

        while (!_vad.IsEmpty())
        {
            var seg = _vad.Front();
            var pcm = seg.Samples;
            if (pcm is { Length: > 0 })
                segments.Add(new AudioSegment(pcm, SampleRate, seg.Start / (double)SampleRate));
            _vad.Pop();
        }
        return segments;
    }

    public string Transcribe(AudioSegment segment)
    {
        using var stream = _asr.CreateStream();
        stream.AcceptWaveform(segment.SampleRate, segment.Samples);
        _asr.Decode(stream);
        return stream.Result.Text?.Trim() ?? "";
    }

    public (float[] Samples, int SampleRate) Synthesize(string text, string languageCode)
    {
        var gen = new OfflineTtsGenerationConfig
        {
            Sid = languageCode is "zh" or "yue" ? KokoroZhSid : KokoroEnSid,
            Speed = 1.0f,
            SilenceScale = 0.2f,
        };
        // 1.13.8 has no overload without a callback (docs/06 §7 pitfall 4): returning
        // 1 means "keep going"; the callback body has nothing else to do here.
        OfflineTtsCallbackProgressWithArg keepGoing = (samples, n, progress, arg) => 1;

        var audio = _tts.GenerateWithConfig(text, gen, keepGoing);
        return audio is null || audio.Samples.Length == 0
            ? (Array.Empty<float>(), 0)
            : (audio.Samples, audio.SampleRate);
    }

    public void Dispose()
    {
        _vad.Dispose();
        _asr.Dispose();
        _tts.Dispose();
    }
}
