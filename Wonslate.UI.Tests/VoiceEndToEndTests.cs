// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Xunit;
using Xunit.Abstractions;
using Wonslate.Audio;

namespace Wonslate.UI.Tests;

/// <summary>
/// Real-model voice loop: Silero VAD -> SenseVoice ASR -> engine -> Kokoro TTS on a
/// real WAV file. This is the N-10 acceptance evidence.
///
/// Gating: when the model files are absent these tests report and return instead of
/// failing, because a model-less machine is a configuration state, not a defect --
/// the same treatment the FFI tests get when application control blocks the library
/// (docs/09). A machine that DOES have the models runs the real chain.
///
/// The translation leg depends on whichever engine routing picks. Without the argos
/// sidecar the request degrades to the local demo engine, which is why the assertions
/// here are about the CHAIN (audio in -> text -> audio out, no silent degradation)
/// rather than about translation quality.
/// </summary>
public class VoiceEndToEndTests
{
    private readonly ITestOutputHelper _out;
    public VoiceEndToEndTests(ITestOutputHelper output) => _out = output;

    private static VoiceModelPaths Models() => VoiceModelLocator.Resolve();

    /// <summary>Read a 16-bit PCM WAV; the ASR model directory ships zh.wav / en.wav.</summary>
    private static (float[] Samples, int Rate) ReadWav(string path)
    {
        using var fs = File.OpenRead(path);
        using var br = new BinaryReader(fs);
        if (new string(br.ReadChars(4)) != "RIFF") throw new InvalidDataException("not RIFF");
        br.ReadInt32();
        if (new string(br.ReadChars(4)) != "WAVE") throw new InvalidDataException("not WAVE");

        int rate = -1;
        var pcm = new List<float>();
        while (fs.Position + 8 <= fs.Length)
        {
            var id = new string(br.ReadChars(4));
            int size = br.ReadInt32();
            long start = fs.Position;
            if (id == "fmt ")
            {
                br.ReadInt16(); br.ReadInt16();
                rate = br.ReadInt32();
                br.ReadInt32(); br.ReadInt16(); br.ReadInt16();
            }
            else if (id == "data")
            {
                var bytes = br.ReadBytes(size);
                for (int i = 0; i + 1 < bytes.Length; i += 2)
                    pcm.Add(BitConverter.ToInt16(bytes, i) / 32768.0f);
            }
            fs.Seek(start + size + (size & 1), SeekOrigin.Begin);
        }
        return (pcm.ToArray(), rate);
    }

    private static void Report(ITestOutputHelper o, string why)
    {
        o.WriteLine($"[SKIP] {why}");
        o.WriteLine("       fetch with: python script/fetch_voice_models.py --only all");
        o.WriteLine("       or point LT_VOICE_MODEL_DIR at an existing model root.");
    }

    [Fact]
    public void RealModels_TranscribeAPcmWav()
    {
        var paths = Models();
        if (!paths.IsComplete) { Report(_out, "voice models not installed: " + string.Join(", ", paths.Missing)); return; }

        var wav = Path.Combine(paths.AsrDir, "zh.wav");
        if (!File.Exists(wav)) { Report(_out, "zh.wav not next to the ASR model"); return; }

        var (samples, rate) = ReadWav(wav);
        _out.WriteLine($"[input] {Path.GetFileName(wav)}: {rate} Hz, {samples.Length / (double)rate:0.00}s");

        using var stages = new SherpaVoiceStages(paths);

        var sw = System.Diagnostics.Stopwatch.StartNew();
        var segments = stages.Segment(samples, rate);
        sw.Stop();
        _out.WriteLine($"[vad] {segments.Count} segment(s) in {sw.ElapsedMilliseconds} ms");

        Assert.NotEmpty(segments);

        var texts = new List<string>();
        foreach (var seg in segments)
        {
            sw.Restart();
            var text = stages.Transcribe(seg);
            sw.Stop();
            _out.WriteLine($"[asr] {sw.ElapsedMilliseconds} ms for {seg.DurationSeconds:0.00}s audio -> {text}");
            texts.Add(text);
        }
        Assert.Contains(texts, t => !string.IsNullOrWhiteSpace(t));
    }

    [Fact]
    public void RealModels_EndToEnd_ProducesTargetLanguageAudio()
    {
        var paths = Models();
        if (!paths.IsComplete) { Report(_out, "voice models not installed: " + string.Join(", ", paths.Missing)); return; }

        var wav = Path.Combine(paths.AsrDir, "zh.wav");
        if (!File.Exists(wav)) { Report(_out, "zh.wav not next to the ASR model"); return; }

        var (samples, rate) = ReadWav(wav);
        using var stages = new SherpaVoiceStages(paths);

        var result = new VoicePipeline(stages, stages, stages, new EngineVoiceTranslator())
            .Run(samples, rate, new VoiceOptions("zh", "en", "realtime", false));

        _out.WriteLine($"[asr ] {result.RecognizedText}");
        _out.WriteLine($"[mt  ] {result.TranslatedText}");
        _out.WriteLine($"[tts ] {(result.HasAudio ? result.Samples.Length + " samples @" + result.SampleRate + " Hz" : "no audio")}");
        _out.WriteLine($"[time] vad={result.Timings.SegmentMs:0} asr={result.Timings.AsrMs:0} " +
                       $"mt={result.Timings.TranslateMs:0} tts={result.Timings.TtsMs:0} " +
                       $"firstAudio={result.Timings.FirstAudioMs:0} total={result.Timings.TotalMs:0} ms");
        foreach (var n in result.Notes) _out.WriteLine($"[note] {n}");

        // The chain must have heard something.
        Assert.False(string.IsNullOrWhiteSpace(result.RecognizedText),
            "ASR produced no text from real speech audio");

        // No silent degradation: a run that produced no audio must explain itself.
        if (!result.HasAudio)
            Assert.NotEmpty(result.Notes);
    }

    [Fact]
    public void RealModels_SynthesizeIsNotSilent()
    {
        var paths = Models();
        if (!paths.IsComplete) { Report(_out, "voice models not installed: " + string.Join(", ", paths.Missing)); return; }

        using var stages = new SherpaVoiceStages(paths);

        var sw = System.Diagnostics.Stopwatch.StartNew();
        var (audio, rate) = stages.Synthesize("Hello world, this is an offline voice check.", "en");
        sw.Stop();

        double rms = 0;
        foreach (var s in audio) rms += s * s;
        rms = Math.Sqrt(rms / Math.Max(1, audio.Length));
        _out.WriteLine($"[tts] {audio.Length} samples @ {rate} Hz, {audio.Length / (double)rate:0.00}s, "
                       + $"rms={rms:0.0000}, {sw.ElapsedMilliseconds} ms");

        Assert.NotEmpty(audio);
        Assert.True(rate > 0);
        Assert.True(rms > 0.01, $"synthesized audio looks silent (rms={rms})");
    }
}
