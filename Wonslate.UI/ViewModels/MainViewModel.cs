// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.ComponentModel;
using System.Runtime.CompilerServices;
using Wonslate.Interop;

namespace Wonslate.ViewModels;

/// <summary>Main-window ViewModel: binds input, mode, the privacy toggle and the translation chain.</summary>
public sealed class MainViewModel : INotifyPropertyChanged
{
    private string _input = "Hello there";
    private string _output = "";
    private bool _isBusy;
    private bool _privacySensitive;
    private string _mode = "realtime";
    private string _engineLabel = "";
    private string _sourceLang = "en";
    private string _targetLang = "zh";

    public string Input
    {
        get => _input;
        set { _input = value; OnPropertyChanged(); }
    }

    public string Output
    {
        get => _output;
        private set { _output = value; OnPropertyChanged(); }
    }

    public bool IsBusy
    {
        get => _isBusy;
        private set { _isBusy = value; OnPropertyChanged(); }
    }

    public bool PrivacySensitive
    {
        get => _privacySensitive;
        set { _privacySensitive = value; OnPropertyChanged(); }
    }

    public string Mode
    {
        get => _mode;
        set { _mode = value; OnPropertyChanged(); }
    }

    /// <summary>Current routing/engine label (includes TM hit state).</summary>
    public string EngineLabel
    {
        get => _engineLabel;
        private set { _engineLabel = value; OnPropertyChanged(); }
    }

    public string EngineVersion => EngineNative.Version();

    /// <summary>Source-language code that asks the engine to detect it (pipeline.rs: "auto" or empty).</summary>
    public const string AutoDetectCode = "auto";

    /// <summary>One selectable language: the engine code plus the label shown in the UI.</summary>
    public sealed record LangOption(string Code, string DisplayName);

    /// <summary>
    /// Languages the router treats as common (router.rs is_common_pair). Selecting one
    /// still needs the matching local model to translate offline; without it the
    /// pipeline reports an explicit fallback instead of pretending to succeed.
    /// </summary>
    private static readonly LangOption[] RealLanguages =
    {
        new LangOption("zh", "中文"),
        new LangOption("en", "English"),
        new LangOption("ja", "日本語"),
        new LangOption("ko", "한국어"),
        new LangOption("fr", "Français"),
        new LangOption("de", "Deutsch"),
        new LangOption("es", "Español"),
        new LangOption("ru", "Русский"),
        new LangOption("pt", "Português"),
        new LangOption("it", "Italiano"),
        new LangOption("ar", "العربية"),
    };

    /// <summary>Concrete languages, offered for both sides of the pair.</summary>
    public IReadOnlyList<LangOption> Languages => RealLanguages;

    /// <summary>Source choices: auto-detection first, then every concrete language.</summary>
    public IReadOnlyList<LangOption> SourceLanguages { get; } =
        new[] { new LangOption(AutoDetectCode, "自动检测") }.Concat(RealLanguages).ToArray();

    /// <summary>Source language code (a concrete code, or "auto" to let the engine detect it).</summary>
    public string SourceLang
    {
        get => _sourceLang;
        set
        {
            _sourceLang = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(CanExchangeLanguages));
        }
    }

    /// <summary>Target language code (always concrete: nothing can be translated into "auto").</summary>
    public string TargetLang
    {
        get => _targetLang;
        set { _targetLang = value; OnPropertyChanged(); }
    }

    /// <summary>Swapping needs a concrete source; "auto" has no meaningful target side.</summary>
    public bool CanExchangeLanguages => _sourceLang != AutoDetectCode;

    /// <summary>Swap source and target languages (no-op while the source is auto-detected).</summary>
    public void ExchangeLanguages()
    {
        if (!CanExchangeLanguages) return;
        (_sourceLang, _targetLang) = (_targetLang, _sourceLang);
        OnPropertyChanged(nameof(SourceLang));
        OnPropertyChanged(nameof(TargetLang));
    }

    /// <summary>Build the v2.0 pipeline request JSON (follows SourceLang/TargetLang; testable).</summary>
    public string BuildRequestJson(string text) =>
        System.Text.Json.JsonSerializer.Serialize(new
        {
            input = text,
            source_lang = SourceLang,
            target_lang = TargetLang,
            mode = Mode == "realtime" ? "realtime" : "full",
            privacy = PrivacySensitive,
            use_tm = true,
        });

    /// <summary>
    /// Run one translation through the v2.0 full pipeline (TM + routing + distillation).
    /// </summary>
    public void Translate()
    {
        var text = Input?.Trim() ?? "";
        if (text.Length == 0) { Output = ""; return; }

        IsBusy = true;
        try
        {
            // Build the JSON request: the pair comes from the UI-selected SourceLang/TargetLang
            var req = BuildRequestJson(text);

            var json = EngineNative.TranslateFull(req);
            var result = EngineNative.TranslateResponseDto.FromJson(json);

            // surface where the result came from
            var srcLabel = result.Source switch
            {
                "tm_hit"      => "TM 命中",
                "local"       => "本地引擎",
                "ai_upgraded" => "AI 精译",
                "fallback"    => "兜底降级",
                _             => result.Source,
            };
            EngineLabel = $"[{srcLabel}] {result.Engine} · {result.LatencyMs}ms";

            Output = result.Ok
                ? result.Output ?? "(无输出)"
                : $"[{result.Error}] {result.Message}";
        }
        catch (Exception ex)
        {
            EngineLabel = "调用失败";
            Output = $"异常: {ex.Message}";
        }
        finally
        {
            IsBusy = false;
        }
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void OnPropertyChanged([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}
