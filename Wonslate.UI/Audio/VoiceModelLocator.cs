// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.IO;

namespace Wonslate.Audio;

/// <summary>
/// Resolved voice-model locations plus an explicit list of what is missing.
///
/// "Missing" is a first-class value rather than an exception: a machine without the
/// models must be able to say so in the UI instead of looking like a broken feature.
/// </summary>
public sealed record VoiceModelPaths(string? VadModel, string AsrDir, string TtsDir, IReadOnlyList<string> Missing)
{
    public bool IsComplete => Missing.Count == 0;
}

/// <summary>
/// Finds the Silero VAD, SenseVoice ASR and Kokoro TTS assets on disk.
///
/// Root resolution mirrors translator-engine/src/config.rs (and
/// sidecar/ct2_sidecar.py::default_model_dir): LT_DATA_DIR / WONSLATE_DATA_DIR wins,
/// otherwise the per-OS user data directory, then "models". The env reader is
/// injectable so this stays hermetic in tests.
///
/// Overrides, LT_* first for backward compatibility then the WONSLATE_* brand
/// spelling; the bare ASR_MODEL_DIR / TTS_MODEL_DIR names are also honoured because
/// AsrDemo / TtsDemo already documented them (docs/06 §7 pitfall 9 - the model
/// directory used to be hardcoded, and every entry point is supposed to read it
/// through configuration instead).
/// </summary>
public static class VoiceModelLocator
{
    private const string AsrFolder = "sense-voice";
    private const string TtsFolder = "kokoro-int8-multi-lang-v1_1";
    private const string VadFolder = "silero-vad";

    /// <summary>ASR model files that must exist for SenseVoice to load.</summary>
    internal static readonly string[] AsrRequired = { "model.int8.onnx", "tokens.txt" };

    /// <summary>TTS model files that must exist for Kokoro to load (docs/06 §7 pitfall 7).</summary>
    internal static readonly string[] TtsRequired =
        { "model.int8.onnx", "voices.bin", "tokens.txt", "espeak-ng-data" };

    public static VoiceModelPaths Resolve() => Resolve(Environment.GetEnvironmentVariable);

    public static VoiceModelPaths Resolve(Func<string, string?> env)
    {
        var root = First(
            env("LT_VOICE_MODEL_DIR"), env("WONSLATE_VOICE_MODEL_DIR")) ?? DefaultModelsRoot(env);

        var asrDir = First(env("ASR_MODEL_DIR"), env("LT_ASR_MODEL_DIR"), env("WONSLATE_ASR_MODEL_DIR"))
                     ?? Path.Combine(root, AsrFolder);

        // Two Kokoro layouts exist in the wild: the flat <root>/kokoro-... used by the
        // fetch script, and the historical <root>/kokoro/kokoro-... nesting (docs/06 §4.2).
        var ttsDir = First(env("TTS_MODEL_DIR"), env("LT_TTS_MODEL_DIR"), env("WONSLATE_TTS_MODEL_DIR"))
                     ?? FirstExisting(
                         Path.Combine(root, TtsFolder),
                         Path.Combine(root, "kokoro", TtsFolder))
                     ?? Path.Combine(root, TtsFolder);

        var vadModel = First(env("LT_VAD_MODEL"), env("WONSLATE_VAD_MODEL"))
                       ?? FirstExisting(
                           Path.Combine(root, VadFolder, "silero_vad.onnx"),
                           Path.Combine(asrDir, "silero_vad.onnx"))
                       ?? Path.Combine(root, VadFolder, "silero_vad.onnx");

        var missing = new List<string>();
        if (!File.Exists(vadModel)) missing.Add("VAD (silero_vad.onnx)");
        foreach (var f in AsrRequired)
            if (!File.Exists(Path.Combine(asrDir, f))) { missing.Add($"ASR ({f})"); break; }
        foreach (var f in TtsRequired)
            if (!Path.Exists(Path.Combine(ttsDir, f))) { missing.Add($"TTS ({f})"); break; }

        return new VoiceModelPaths(vadModel, asrDir, ttsDir, missing);
    }

    /// <summary>Data root's sibling "models" directory, per the shared data-dir rule.</summary>
    internal static string DefaultModelsRoot(Func<string, string?> env)
    {
        var overrideDir = First(env("LT_DATA_DIR"), env("WONSLATE_DATA_DIR"));
        if (overrideDir is not null) return Path.Combine(overrideDir, "models");

        if (OperatingSystem.IsWindows())
        {
            var local = env("LOCALAPPDATA");
            return Path.Combine(local ?? ".", "Wonslate", "models");
        }
        if (OperatingSystem.IsMacOS())
        {
            var home = env("HOME") ?? ".";
            return Path.Combine(home, "Library", "Application Support", "Wonslate", "models");
        }
        var xdg = env("XDG_DATA_HOME") ?? Path.Combine(env("HOME") ?? ".", ".local", "share");
        return Path.Combine(xdg, "wonslate", "models");
    }

    private static string? First(params string?[] values)
    {
        foreach (var v in values)
            if (!string.IsNullOrWhiteSpace(v)) return v.Trim();
        return null;
    }

    private static string? FirstExisting(params string[] candidates)
    {
        foreach (var c in candidates)
            if (Path.Exists(c)) return c;
        return null;
    }
}
