// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using SherpaOnnx;

// =====================================================================
//  AsrDemo : 本地离线 ASR 命令行 demo（sherpa-onnx + SenseVoice）
//
//  用法:
//    AsrDemo.exe [wav1 wav2 ...] [-vad]
//    不传 wav 则默认识别模型目录下的 zh.wav / en.wav / mixed_zh_en.wav
//    -vad  : 额外加载 Silero VAD 对输入做语音活动检测（含加载自检）
//  模型目录(默认): models\sense-voice（可用环境变量 ASR_MODEL_DIR 覆盖）
// =====================================================================

string ModelDir = Environment.GetEnvironmentVariable("ASR_MODEL_DIR") ?? @"models\sense-voice";

static float[] ReadWavPcm16(string path, out int sampleRate)
{
    using var fs = File.OpenRead(path);
    using var br = new BinaryReader(fs);
    if (new string(br.ReadChars(4)) != "RIFF") throw new InvalidDataException("not a RIFF file: " + path);
    br.ReadInt32();                       // riff size
    if (new string(br.ReadChars(4)) != "WAVE") throw new InvalidDataException("not WAVE: " + path);

    short audioFormat = -1, channels = -1, bitsPerSample = -1;
    sampleRate = -1;
    var pcm = new List<float>();

    while (fs.Position + 8 <= fs.Length)
    {
        var chunkId = new string(br.ReadChars(4));
        int chunkSize = br.ReadInt32();
        long chunkStart = fs.Position;
        if (chunkId == "fmt ")
        {
            audioFormat = br.ReadInt16();
            channels = br.ReadInt16();
            sampleRate = br.ReadInt32();
            br.ReadInt32();               // byte rate
            br.ReadInt16();               // block align
            bitsPerSample = br.ReadInt16();
        }
        else if (chunkId == "data")
        {
            byte[] bytes = br.ReadBytes(chunkSize);
            if (audioFormat == 1 && bitsPerSample == 16)
            {
                for (int i = 0; i + 1 < bytes.Length; i += 2)
                    pcm.Add(BitConverter.ToInt16(bytes, i) / 32768.0f);
            }
            else if (audioFormat == 3 && bitsPerSample == 32)
            {
                for (int i = 0; i + 3 < bytes.Length; i += 4)
                    pcm.Add(BitConverter.ToSingle(bytes, i));
            }
            else
            {
                throw new NotSupportedException($"unsupported wav fmt={audioFormat} bits={bitsPerSample}");
            }
        }
        // 跳过当前 chunk（对齐到偶数）
        fs.Seek(chunkStart + chunkSize + (chunkSize & 1), SeekOrigin.Begin);
    }

    if (pcm.Count == 0) throw new InvalidDataException("no pcm data: " + path);
    return pcm.ToArray();
}

// ---------- 参数解析 ----------
var positional = new List<string>();
bool doVad = false;
foreach (var a in args)
{
    if (a == "-vad") doVad = true;
    else positional.Add(a);
}

var files = positional.Count > 0
    ? positional
    : new List<string> {
        Path.Combine(ModelDir, "mixed_zh_en.wav"),
        Path.Combine(ModelDir, "zh.wav"),
        Path.Combine(ModelDir, "en.wav"),
    };

// ---------- 构建 OfflineRecognizer (SenseVoice) ----------
var modelPath = Path.Combine(ModelDir, "model.int8.onnx");
var tokensPath = Path.Combine(ModelDir, "tokens.txt");
if (!File.Exists(modelPath) || !File.Exists(tokensPath))
    throw new FileNotFoundException($"模型缺失: {modelPath} / {tokensPath}");

Console.WriteLine($"[model] {modelPath}");
var loadSw = Stopwatch.StartNew();
var config = new OfflineRecognizerConfig();
config.FeatConfig.SampleRate = 16000;
config.FeatConfig.FeatureDim = 80;
config.ModelConfig.SenseVoice = new OfflineSenseVoiceModelConfig
{
    Model = modelPath,
    Language = "auto",
    UseInverseTextNormalization = 1,     // ITN: 数字/标点规整
};
config.ModelConfig.Tokens = tokensPath;
config.ModelConfig.NumThreads = 2;
config.ModelConfig.Debug = 0;
config.ModelConfig.Provider = "cpu";

using var recognizer = new OfflineRecognizer(config);
loadSw.Stop();
Console.WriteLine($"[model load] {loadSw.ElapsedMilliseconds} ms\n");

// ---------- 可选 Silero VAD 自检 ----------
if (doVad)
{
    var vadPath = Path.Combine(ModelDir, "silero_vad.onnx");
    var vadLoad = Stopwatch.StartNew();
    var vadConfig = new VadModelConfig();
    vadConfig.SileroVad = new SileroVadModelConfig { Model = vadPath };
    vadConfig.SampleRate = 16000;
    using var vad = new VoiceActivityDetector(vadConfig, 16.0f);
    vadLoad.Stop();
    Console.WriteLine($"[vad load] {Path.GetFileName(vadPath)}  {vadLoad.ElapsedMilliseconds} ms");
    foreach (var f in files)
    {
        var samples = ReadWavPcm16(f, out int rate);
        vad.Reset();
        vad.AcceptWaveform(samples);
        vad.Flush();
        int segs = 0;
        while (!vad.IsEmpty())
        {
            var seg = vad.Front();
            double dur = seg.Samples != null ? seg.Samples.Length / (double)rate : 0.0;
            Console.WriteLine($"  [vad] {Path.GetFileName(f)} start={seg.Start / (double)rate:0.00}s dur={dur:0.00}s");
            vad.Pop();
            segs++;
        }
        Console.WriteLine($"  [vad total] {Path.GetFileName(f)}: {segs} 段");
    }
    Console.WriteLine();
}

// ---------- 逐文件离线识别 ----------
foreach (var f in files)
{
    if (!File.Exists(f)) { Console.WriteLine($"[skip] 文件不存在: {f}"); continue; }
    var samples = ReadWavPcm16(f, out int rate);
    double duration = samples.Length / (double)rate;

    var sw = Stopwatch.StartNew();
    using var stream = recognizer.CreateStream();
    stream.AcceptWaveform(rate, samples);
    recognizer.Decode(stream);
    var text = stream.Result.Text;
    sw.Stop();

    double rtf = sw.Elapsed.TotalSeconds / Math.Max(duration, 1e-6);
    Console.WriteLine($"--- {Path.GetFileName(f)} ---");
    Console.WriteLine($"    wav: {rate} Hz, {duration:0.00}s, {samples.Length} samples");
    Console.WriteLine($"    text: {text}");
    Console.WriteLine($"    decode: {sw.ElapsedMilliseconds} ms  (RTF = {rtf:0.000})");
    Console.WriteLine();
}
Console.WriteLine("done.");
