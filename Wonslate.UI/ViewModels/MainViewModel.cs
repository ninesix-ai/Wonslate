// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.ComponentModel;
using System.Runtime.CompilerServices;
using Wonslate.Interop;

namespace Wonslate.ViewModels;

/// <summary>主窗口 ViewModel：绑定输入、模式、隐私开关与翻译链路。</summary>
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

    /// <summary>当前生效路由/引擎说明（含 TM 命中状态）。</summary>
    public string EngineLabel
    {
        get => _engineLabel;
        private set { _engineLabel = value; OnPropertyChanged(); }
    }

    public string EngineVersion => EngineNative.Version();

    /// <summary>可选语言（当前离线引擎支持 英↔中；更多语言对随 Phase 2 引擎接入而扩展）。</summary>
    public sealed record LangOption(string Code, string DisplayName);
    public IReadOnlyList<LangOption> Languages { get; } = new[]
    {
        new LangOption("en", "English"),
        new LangOption("zh", "中文"),
    };

    /// <summary>源语言代码（如 en / zh）。</summary>
    public string SourceLang
    {
        get => _sourceLang;
        set { _sourceLang = value; OnPropertyChanged(); }
    }

    /// <summary>目标语言代码。</summary>
    public string TargetLang
    {
        get => _targetLang;
        set { _targetLang = value; OnPropertyChanged(); }
    }

    /// <summary>交换源/目标语言。</summary>
    public void ExchangeLanguages()
    {
        (_sourceLang, _targetLang) = (_targetLang, _sourceLang);
        OnPropertyChanged(nameof(SourceLang));
        OnPropertyChanged(nameof(TargetLang));
    }

    /// <summary>构建 v2.0 全功能流水线的请求 JSON（随 SourceLang/TargetLang 变化，可测）。</summary>
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
    /// 执行一次翻译：走 v2.0 全功能流水线（TM + 路由 + 蒸馏）。
    /// </summary>
    public void Translate()
    {
        var text = Input?.Trim() ?? "";
        if (text.Length == 0) { Output = ""; return; }

        IsBusy = true;
        try
        {
            // 构建 JSON 请求：语言对来自 UI 选择的 SourceLang/TargetLang
            var req = BuildRequestJson(text);

            var json = EngineNative.TranslateFull(req);
            var result = EngineNative.TranslateResponseDto.FromJson(json);

            // 展示命中来源
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
