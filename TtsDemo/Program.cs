// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
// TtsDemo: 本地离线 TTS 命令行 demo（sherpa-onnx + Kokoro v1.1 多语言模型，含中文普通话）
//
// 用法:
//   dotnet run -c Release                 # 跑内置 3 条测试文本，输出到当前目录 wav
//   dotnet run -c Release -- --text "<文本>" [--sid N] [--speed 1.0] [--out <路径.wav>]
//   dotnet run -c Release -- --text "hello" --out out.wav
//
// 中文普通话音色参考 sid（Kokoro 多语言 v1.0/v1.1 一致）:
//   45/46/47/48/49/50/51/52 均为中文女/男声，52 对中英混排支持最好
//
using SherpaOnnx;
using System.Runtime.InteropServices;

class TtsDemo
{
    // 模型目录（M2 固定指向本机落盘路径，可被 args 覆盖）
    static string ModelDir = Environment.GetEnvironmentVariable("TTS_MODEL_DIR") ?? "models/kokoro-int8-multi-lang-v1_1";

    static int Main(string[] args)
    {
        // ---- 解析简单命令行 ----
        Dictionary<string, string> kv = new();
        for (int i = 0; i < args.Length - 1; i++)
        {
            if (args[i].StartsWith("--"))
                kv[args[i]] = args[i + 1];
        }

        string? text = kv.GetValueOrDefault("--text");
        int sid = kv.ContainsKey("--sid") ? int.Parse(kv["--sid"]) : 50;
        float speed = kv.ContainsKey("--speed") ? float.Parse(kv["--speed"]) : 1.0f;
        string? outPath = kv.GetValueOrDefault("--out");
        if (kv.ContainsKey("--model-dir"))
            ModelDir = kv["--model-dir"];

        OfflineTts? tts = null;
        try
        {
            tts = BuildTts(ModelDir);
        }
        catch (Exception e)
        {
            Console.Error.WriteLine($"[TtsDemo] 初始化 TTS 失败: {e.Message}");
            return 1;
        }

        if (!string.IsNullOrEmpty(text))
        {
            // 单条合成（--text）
            string savePath = string.IsNullOrEmpty(outPath)
                ? Path.Combine(Environment.CurrentDirectory, $"tts_sid{sid}.wav")
                : outPath;
            return SynthesizeOne(tts, text, sid, speed, savePath) ? 0 : 2;
        }

        // ---- 默认回归：3 条（含中英混排）----
        var cases = new (string text, int sid)[]
        {
            ("你好，欢迎使用本地离线语音合成。这是第一条中文测试。", 50),
            ("Are you OK 是雷军 2015 年 4 月小米在印度发布新品时说的，"
             + "他还说过：I am very happy to be in China。中英混排合成没有问题，加油！", 52),
            ("根据第七次全国人口普查，我国总人口有十四亿四千多万人。"
             + "遇到困难，请拨打 110 或者测试号码 12301000000。", 48),
        };

        Console.WriteLine($"=== Kokoro TTS 回归测试, 模型目录: {ModelDir}, speed={speed} ===");
        int fail = 0;
        for (int i = 0; i < cases.Length; i++)
        {
            string p = Path.Combine(Environment.CurrentDirectory, $"tts_case{i + 1}_sid{cases[i].sid}.wav");
            if (!SynthesizeOne(tts, cases[i].text, cases[i].sid, speed, p))
                fail++;
        }
        Console.WriteLine(fail == 0 ? "全部合成成功" : $"{fail} 条失败");
        return fail == 0 ? 0 : 3;
    }

    static OfflineTts BuildTts(string dir)
    {
        var config = new OfflineTtsConfig();
        config.Model.Kokoro.Model     = Path.Combine(dir, "model.int8.onnx");
        config.Model.Kokoro.Voices    = Path.Combine(dir, "voices.bin");
        config.Model.Kokoro.Tokens    = Path.Combine(dir, "tokens.txt");
        config.Model.Kokoro.DataDir   = Path.Combine(dir, "espeak-ng-data");
        config.Model.Kokoro.Lexicon   =
            Path.Combine(dir, "lexicon-us-en.txt") + "," +
            Path.Combine(dir, "lexicon-zh.txt");
        config.Model.NumThreads = 2;
        config.Model.Provider   = "cpu";
        return new OfflineTts(config);
    }

    static bool SynthesizeOne(OfflineTts tts, string text, int sid, float speed, string outPath)
    {
        Console.WriteLine($"\n--- 文本: {text}");
        Console.WriteLine($"    音色 sid={sid}, speed={speed}");

        var gen = new OfflineTtsGenerationConfig
        {
            Sid = sid,
            Speed = speed,
            SilenceScale = 0.2f,
        };

        OfflineTtsCallbackProgressWithArg cb = (IntPtr samples, int n, float progress, IntPtr arg) => 1;
        var audio = tts.GenerateWithConfig(text, gen, cb);
        if (audio == null || audio.Samples.Length == 0)
        {
            Console.WriteLine("    合成失败: samples 为空");
            return false;
        }

        string dir = Path.GetDirectoryName(Path.GetFullPath(outPath))!;
        Directory.CreateDirectory(dir);
        bool ok;
        try
        {
            ok = audio.SaveToWaveFile(outPath);
        }
        catch (Exception e)
        {
            Console.WriteLine($"    保存异常: {e.Message}");
            return false;
        }
        if (!ok || !File.Exists(outPath))
        {
            Console.WriteLine("    SaveToWaveFile 返回 false");
            return false;
        }

        // ---- 校验：时长 / 采样率 / RMS 非静音 ----
        var fi = new FileInfo(outPath);
        double duration = (double)audio.Samples.Length / audio.SampleRate;
        float rms = MathF.Sqrt(audio.Samples.Sum(f => f * f) / Math.Max(1, audio.Samples.Length));
        string valid = (duration >= 0.5 && rms > 0.01 && duration < 600) ? "OK(非静音,时长合理)" : "疑似异常";
        Console.WriteLine($"    => 输出 {outPath}");
        Console.WriteLine($"       大小={fi.Length} 字节, 采样率={audio.SampleRate} Hz, "
                        + $"样本数={audio.Samples.Length}, 时长={duration:F2}s, "
                        + $"RMS={rms:F4}, 校验: {valid}");
        return valid == "OK(非静音,时长合理)";
    }
}
