// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System;
using System.ComponentModel;
using System.Threading;
using System.Threading.Tasks;
using Wonslate.ViewModels;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// MainViewModel state-machine tests. Covers only paths that never touch the FFI:
///  -  - property setters raise PropertyChanged correctly
///  -  - Translate() early-exits on empty input (no FFI)
///  -  - initial default state (the P0 demo text)
///  The P/Invoke translation paths are covered by the Python FFI smoke (tests/test_phase1_ffi.py).
/// </summary>
public class MainViewModelTests
{
    // ---- Initial state --------------------------------------------------

    [Fact]
    public void NewViewModel_HasP0DemoInput()
    {
        var vm = new MainViewModel();
        Assert.Equal("Hello there", vm.Input);   // demo phrase; the demo engine yields"你好"
        Assert.Equal("", vm.Output);
        Assert.Equal("realtime", vm.Mode);
        Assert.False(vm.PrivacySensitive);
        Assert.False(vm.IsBusy);
    }

    // ---- PropertyChanged contract --------------------------------

    [Fact]
    public void Input_Setter_RaisesPropertyChanged()
    {
        var vm = new MainViewModel();
        string? raised = null;
        vm.PropertyChanged += (_, e) => raised = e.PropertyName;

        vm.Input = "world";

        Assert.Equal(nameof(MainViewModel.Input), raised);
        Assert.Equal("world", vm.Input);
    }

    [Fact]
    public void Input_SameValue_StillRaises()
    {
        // The current implementation does not de-duplicate equal values; every assignment notifies. The test pins this down.
        var vm = new MainViewModel { Input = "hello" };
        int count = 0;
        vm.PropertyChanged += (_, e) => { if (e.PropertyName == nameof(MainViewModel.Input)) count++; };

        vm.Input = "hello";

        Assert.Equal(1, count);
    }

    [Theory]
    [InlineData(nameof(MainViewModel.Input))]
    [InlineData(nameof(MainViewModel.PrivacySensitive))]
    [InlineData(nameof(MainViewModel.Mode))]
    public void Property_HasPublicSetter(string propName)
    {
        // Two-way binding requires a public setter
        var prop = typeof(MainViewModel).GetProperty(propName);
        Assert.NotNull(prop);
        Assert.True(prop!.CanWrite, $"{propName} 应可写以支持 WPF 双向绑定");
    }

    [Theory]
    [InlineData(nameof(MainViewModel.Output))]
    [InlineData(nameof(MainViewModel.IsBusy))]
    [InlineData(nameof(MainViewModel.EngineLabel))]
    public void ComputedProperty_HasPrivateSetter(string propName)
    {
        // Output-side properties: written by VM internals, read-only for the UI, protected from stray edits
        var prop = typeof(MainViewModel).GetProperty(propName);
        Assert.NotNull(prop);
        Assert.True(prop!.CanRead);
        var setter = prop.GetSetMethod(nonPublic: true);
        Assert.NotNull(setter);
        Assert.False(setter!.IsPublic, $"{propName} setter 应为 private");
    }

    // ---- Translate() early-exit path (no FFI)----------------------------

    [Fact]
    public void Translate_EmptyInput_ClearsOutput_AndSkipsFfi()
    {
        var vm = new MainViewModel { Input = "" };

        vm.Translate();   // empty input exits before the FFI

        Assert.Equal("", vm.Output);
        Assert.False(vm.IsBusy);   // never entered the busy state
    }

    [Fact]
    public void Translate_WhitespaceOnlyInput_SameAsEmpty()
    {
        var vm = new MainViewModel { Input = "   \t\n  " };

        vm.Translate();

        Assert.Equal("", vm.Output);
    }

    [Fact]
    public void Translate_NullInput_SafeGuarded()
    {
        var vm = new MainViewModel();
        vm.Input = null!;   // force null to hit the ?.Trim() defensive path

        vm.Translate();

        Assert.Equal("", vm.Output);
    }

    // ---- INotifyPropertyChanged contract ----------------------------------

    [Fact]
    public void ImplementsINotifyPropertyChanged()
    {
        // the bare minimum WPF binding requires
        Assert.IsAssignableFrom<INotifyPropertyChanged>(new MainViewModel());
    }

    [Fact]
    public void Mode_Changes_PreserveViaTwoWayBindingContract()
    {
        var vm = new MainViewModel();
        vm.Mode = "full";
        Assert.Equal("full", vm.Mode);
        vm.Mode = "realtime";
        Assert.Equal("realtime", vm.Mode);
    }

    [Fact]
    public void PrivacySensitive_ToggleRaisesEvent()
    {
        var vm = new MainViewModel();
        string? propName = null;
        vm.PropertyChanged += (_, e) => propName = e.PropertyName;

        vm.PrivacySensitive = true;

        Assert.Equal(nameof(MainViewModel.PrivacySensitive), propName);
        Assert.True(vm.PrivacySensitive);
    }

    // ---- Language-pair decoupling--------------------------------------

    [Fact]
    public void NewViewModel_DefaultLanguages_AreEnToZh()
    {
        var vm = new MainViewModel();
        Assert.Equal("en", vm.SourceLang);
        Assert.Equal("zh", vm.TargetLang);
    }

    [Fact]
    public void Languages_ContainsEnglishAndChinese()
    {
        var vm = new MainViewModel();
        Assert.Contains(vm.Languages, l => l.Code == "en");
        Assert.Contains(vm.Languages, l => l.Code == "zh");
    }

    [Fact]
    public void BuildRequestJson_DefaultsEnToZh()
    {
        var vm = new MainViewModel();
        var json = vm.BuildRequestJson("hello");
        Assert.Contains("\"source_lang\":\"en\"", json);
        Assert.Contains("\"target_lang\":\"zh\"", json);
    }

    [Fact]
    public void BuildRequestJson_ReflectsSelectedLanguages()
    {
        var vm = new MainViewModel { SourceLang = "zh", TargetLang = "en" };
        var json = vm.BuildRequestJson("x");
        Assert.Contains("\"source_lang\":\"zh\"", json);
        Assert.Contains("\"target_lang\":\"en\"", json);
    }

    [Fact]
    public void ExchangeLanguages_SwapsSourceAndTarget_AndRaises()
    {
        var vm = new MainViewModel();   // en -> zh
        var raised = new System.Collections.Generic.List<string?>();
        vm.PropertyChanged += (_, e) => raised.Add(e.PropertyName);

        vm.ExchangeLanguages();

        Assert.Equal("zh", vm.SourceLang);
        Assert.Equal("en", vm.TargetLang);
        Assert.Contains(nameof(MainViewModel.SourceLang), raised);
        Assert.Contains(nameof(MainViewModel.TargetLang), raised);
    }

    // ---- Multi-language coverage (N-05) ----------------------------------

    [Fact]
    public void Languages_OffersAtLeastElevenCommonLanguages()
    {
        // Mirrors router.rs is_common_pair. Whether a pair can actually translate
        // offline depends on the local model being installed; an uninstalled pair
        // falls back explicitly rather than silently.
        var vm = new MainViewModel();
        Assert.True(vm.Languages.Count >= 11, $"expected >= 11 languages, got {vm.Languages.Count}");
        foreach (var code in new[] { "zh", "en", "ja", "ko", "fr", "de", "es", "ru", "pt", "it", "ar" })
        {
            Assert.Contains(vm.Languages, l => l.Code == code);
        }
    }

    [Fact]
    public void Languages_NeverOfferAutoDetectAsATarget()
    {
        // Nothing can be translated into "auto"; only the source side offers it.
        var vm = new MainViewModel();
        Assert.DoesNotContain(vm.Languages, l => l.Code == MainViewModel.AutoDetectCode);
    }

    [Fact]
    public void SourceLanguages_OfferAutoDetectPlusEveryConcreteLanguage()
    {
        var vm = new MainViewModel();
        Assert.Equal(MainViewModel.AutoDetectCode, vm.SourceLanguages[0].Code);
        Assert.Equal(vm.Languages.Count + 1, vm.SourceLanguages.Count);
    }

    [Fact]
    public void BuildRequestJson_ReflectsAutoDetectSource()
    {
        var vm = new MainViewModel { SourceLang = MainViewModel.AutoDetectCode, TargetLang = "ja" };
        var json = vm.BuildRequestJson("hello");
        Assert.Contains("\"source_lang\":\"auto\"", json);
        Assert.Contains("\"target_lang\":\"ja\"", json);
    }

    [Fact]
    public void BuildRequestJson_ReflectsAnExtendedLanguagePair()
    {
        // A non en/zh pair must reach the engine request untouched.
        var vm = new MainViewModel { SourceLang = "de", TargetLang = "ar" };
        var json = vm.BuildRequestJson("hallo");
        Assert.Contains("\"source_lang\":\"de\"", json);
        Assert.Contains("\"target_lang\":\"ar\"", json);
    }

    [Fact]
    public void ExchangeLanguages_IsDisabledWhileSourceIsAutoDetect()
    {
        var vm = new MainViewModel { SourceLang = MainViewModel.AutoDetectCode, TargetLang = "zh" };
        Assert.False(vm.CanExchangeLanguages);

        vm.ExchangeLanguages();   // must never put "auto" on the target side

        Assert.Equal(MainViewModel.AutoDetectCode, vm.SourceLang);
        Assert.Equal("zh", vm.TargetLang);
    }

    [Fact]
    public void SourceLang_ToAutoDetect_RaisesCanExchangeLanguages()
    {
        var vm = new MainViewModel();
        var raised = new System.Collections.Generic.List<string?>();
        vm.PropertyChanged += (_, e) => raised.Add(e.PropertyName);

        vm.SourceLang = MainViewModel.AutoDetectCode;

        Assert.Contains(nameof(MainViewModel.CanExchangeLanguages), raised);
    }

    // ---- TM / glossary management (N-07), FFI-free paths only ----------------

    [Fact]
    public void BuildTmEntryJson_CarriesThePairAsUserConfirmed()
    {
        var json = MainViewModel.BuildTmEntryJson("深度学习", "zh", "deep learning", "en");

        Assert.Contains("\"source_text\":\"深度学习\"", json);
        Assert.Contains("\"source_lang\":\"zh\"", json);
        Assert.Contains("\"target_text\":\"deep learning\"", json);
        Assert.Contains("\"target_lang\":\"en\"", json);
        Assert.Contains("\"engine\":\"manual\"", json);
        Assert.Contains("\"quality\":1", json);
    }

    [Fact]
    public void BuildGlossaryEntryJson_MarksTheEntryAsUserEdited()
    {
        var json = MainViewModel.BuildGlossaryEntryJson("神经网络", "zh", "neural network", "en");

        Assert.Contains("\"source_term\":\"神经网络\"", json);
        Assert.Contains("\"target_term\":\"neural network\"", json);
        // 1.0 beats any distilled confidence, so a later distillation pass cannot overwrite it.
        Assert.Contains("\"confidence\":1", json);
    }

    [Fact]
    public void EffectiveSourceLang_IsConcreteWhenSourceIsExplicit()
    {
        var vm = new MainViewModel();
        Assert.Equal("en", vm.EffectiveSourceLang);
        Assert.True(vm.CanManageGlossary);
    }

    [Fact]
    public void EffectiveSourceLang_StaysEmptyForUnresolvedAutoDetect()
    {
        // Nothing has been translated yet, so "auto" has no concrete code to key TM/glossary writes with.
        var vm = new MainViewModel { SourceLang = MainViewModel.AutoDetectCode };

        Assert.Equal("", vm.EffectiveSourceLang);
        Assert.False(vm.CanManageGlossary);
    }

    [Fact]
    public void SaveCurrentToTm_WithoutOutput_SetsStatusAndSkipsFfi()
    {
        var vm = new MainViewModel { Input = "hello" };

        vm.SaveCurrentToTm();   // no translation yet: must exit before the FFI

        Assert.Contains("没有可存入的译文", vm.StatusMessage);
        Assert.Empty(vm.TmEntries);
    }

    [Fact]
    public void FlagBadSelectedTm_WithoutSelection_SetsStatusAndSkipsFfi()
    {
        var vm = new MainViewModel();

        vm.FlagBadSelectedTm();

        Assert.Contains("请先选择", vm.StatusMessage);
    }

    [Fact]
    public void SaveGlossaryTerm_WithEmptyTerm_SetsStatusAndSkipsFfi()
    {
        var vm = new MainViewModel { GlossarySourceTerm = "  ", GlossaryTargetTerm = "x" };

        vm.SaveGlossaryTerm();

        Assert.Contains("不能为空", vm.StatusMessage);
    }

    [Fact]
    public void SaveGlossaryTerm_UnderAutoDetect_SetsStatusAndSkipsFfi()
    {
        var vm = new MainViewModel
        {
            SourceLang = MainViewModel.AutoDetectCode,
            GlossarySourceTerm = "神经网络",
            GlossaryTargetTerm = "neural network",
        };

        vm.SaveGlossaryTerm();

        Assert.Contains("自动检测", vm.StatusMessage);
    }

    [Fact]
    public void DeleteSelectedGlossaryTerm_WithoutSelection_SetsStatusAndSkipsFfi()
    {
        var vm = new MainViewModel();

        vm.DeleteSelectedGlossaryTerm();

        Assert.Contains("请先选择", vm.StatusMessage);
    }

    [Fact]
    public void StatusMessage_StartsEmpty_AndIsReadOnlyForTheUi()
    {
        var vm = new MainViewModel();
        Assert.Equal("", vm.StatusMessage);

        var prop = typeof(MainViewModel).GetProperty(nameof(MainViewModel.StatusMessage));
        Assert.NotNull(prop);
        Assert.False(prop!.GetSetMethod(nonPublic: true)!.IsPublic);
    }

    [Fact]
    public void ManagementCollections_StartEmpty()
    {
        var vm = new MainViewModel();
        Assert.Empty(vm.TmEntries);
        Assert.Empty(vm.GlossaryEntries);
        Assert.Null(vm.SelectedTmEntry);
        Assert.Null(vm.SelectedGlossaryEntry);
    }

    // ---- Async translation: success / timeout / cancel (N-06) -----------------
    // The engine call is injected, so these three paths are verified without the FFI.

    private const string OkJson = """
        {"ok":true,"engine":"demo","source_lang":"en","target_lang":"zh",
         "input":"hello","output":"你好","source":"local","latency_ms":3,"confidence":0.9}
        """;

    [Fact]
    public void Translate_SyncPath_AppliesTheResponse()
    {
        var vm = new MainViewModel { Input = "hello" };
        vm.InvokeTranslate = _ => OkJson;

        vm.Translate();

        Assert.Equal("你好", vm.Output);
        Assert.Contains("本地引擎", vm.EngineLabel);
        Assert.False(vm.IsBusy);
    }

    [Fact]
    public async Task TranslateAsync_Success_AppliesResponse()
    {
        var vm = new MainViewModel { Input = "hello" };
        vm.InvokeTranslate = _ => OkJson;

        await vm.TranslateAsync();

        Assert.Equal("你好", vm.Output);
        Assert.Contains("本地引擎", vm.EngineLabel);
        Assert.False(vm.IsBusy);
        Assert.True(vm.IsNotBusy);
    }

    [Fact]
    public async Task TranslateAsync_WhileRunning_ReportsBusy()
    {
        var vm = new MainViewModel { Input = "hello" };
        var started = new TaskCompletionSource();
        var release = new TaskCompletionSource<string>();
        vm.InvokeTranslate = _ =>
        {
            started.TrySetResult();
            return release.Task.GetAwaiter().GetResult();
        };

        var task = vm.TranslateAsync();
        await started.Task;

        Assert.True(vm.IsBusy);
        Assert.False(vm.IsNotBusy);   // the translate button is disabled while in flight

        release.SetResult(OkJson);
        await task;

        Assert.False(vm.IsBusy);
        Assert.True(vm.IsNotBusy);
    }

    [Fact]
    public async Task TranslateAsync_Timeout_RetriesOnceThenReportsTimeout()
    {
        var vm = new MainViewModel { Input = "hello", TranslateTimeout = TimeSpan.FromMilliseconds(400) };
        int calls = 0;
        var started = new TaskCompletionSource();
        var release = new ManualResetEventSlim(false);
        vm.InvokeTranslate = _ =>
        {
            Interlocked.Increment(ref calls);
            started.TrySetResult();
            release.Wait(TimeSpan.FromSeconds(10));
            return OkJson;
        };

        try
        {
            var task = vm.TranslateAsync();
            await started.Task;   // the first attempt is running, so the timeout cannot fire early
            await task;

            Assert.Equal(2, calls);              // one attempt + exactly one retry
            Assert.Equal("超时", vm.EngineLabel);
            Assert.Contains("超时", vm.Output);
            Assert.False(vm.IsBusy);
        }
        finally
        {
            release.Set();   // let the abandoned calls finish so no thread is left blocked
        }
    }

    [Fact]
    public async Task TranslateAsync_FirstAttemptTooSlow_RetrySucceeds()
    {
        var vm = new MainViewModel { Input = "hello", TranslateTimeout = TimeSpan.FromMilliseconds(400) };
        int calls = 0;
        var firstStarted = new TaskCompletionSource();
        var releaseFirst = new ManualResetEventSlim(false);
        vm.InvokeTranslate = _ =>
        {
            if (Interlocked.Increment(ref calls) == 1)
            {
                firstStarted.TrySetResult();
                releaseFirst.Wait(TimeSpan.FromSeconds(10));   // overruns the timeout
                return "{}";
            }
            return OkJson;
        };

        try
        {
            var task = vm.TranslateAsync();
            await firstStarted.Task;
            await task;

            Assert.Equal(2, calls);
            Assert.Equal("你好", vm.Output);   // the slow first result is dropped, never applied late
            Assert.False(vm.IsBusy);
        }
        finally
        {
            releaseFirst.Set();
        }
    }

    [Fact]
    public async Task TranslateAsync_Cancelled_ReportsCancellation()
    {
        var vm = new MainViewModel { Input = "hello" };
        var started = new TaskCompletionSource();
        var release = new ManualResetEventSlim(false);
        vm.InvokeTranslate = _ =>
        {
            started.TrySetResult();
            release.Wait(TimeSpan.FromSeconds(10));
            return "{}";
        };

        try
        {
            var task = vm.TranslateAsync();
            await started.Task;

            vm.CancelTranslate();   // what the cancel button does
            await task;

            Assert.Equal("已取消", vm.EngineLabel);
            Assert.Equal("翻译已取消", vm.StatusMessage);
            Assert.False(vm.IsBusy);
        }
        finally
        {
            release.Set();
        }
    }

    [Fact]
    public async Task TranslateAsync_CancelledToken_ReportsCancellation()
    {
        var vm = new MainViewModel { Input = "hello" };
        var release = new ManualResetEventSlim(false);
        vm.InvokeTranslate = _ => { release.Wait(TimeSpan.FromSeconds(10)); return "{}"; };
        using var cts = new CancellationTokenSource();

        try
        {
            var task = vm.TranslateAsync(cts.Token);
            cts.Cancel();
            await task;

            Assert.Equal("已取消", vm.EngineLabel);
        }
        finally
        {
            release.Set();
        }
    }

    [Fact]
    public void CancelTranslate_WithoutInFlightCall_IsNoOp()
    {
        var vm = new MainViewModel();

        vm.CancelTranslate();

        Assert.False(vm.IsBusy);
        Assert.Equal("", vm.Output);
    }

    [Fact]
    public async Task TranslateAsync_ThrowingEngine_ReportsTheException_NotAnEscalation()
    {
        var vm = new MainViewModel { Input = "hello" };
        vm.InvokeTranslate = _ => throw new InvalidOperationException("native library missing");

        await vm.TranslateAsync();

        Assert.Equal("调用失败", vm.EngineLabel);
        Assert.Contains("native library missing", vm.Output);
        Assert.False(vm.IsBusy);
    }

    // ---- Busy / diagnostics surface (N-06) -----------------------------------

    [Fact]
    public void IsNotBusy_StartsTrue_AndHasNoSetter()
    {
        var vm = new MainViewModel();
        Assert.True(vm.IsNotBusy);

        var prop = typeof(MainViewModel).GetProperty(nameof(MainViewModel.IsNotBusy));
        Assert.NotNull(prop);
        Assert.Null(prop!.GetSetMethod(nonPublic: true));
    }

    [Fact]
    public void SetDiagnostics_PublishesAndNotifies()
    {
        var vm = new MainViewModel();
        string? raised = null;
        vm.PropertyChanged += (_, e) =>
        {
            if (e.PropertyName == nameof(MainViewModel.Diagnostics)) raised = e.PropertyName;
        };

        vm.SetDiagnostics("引擎初始化失败：TM_ERROR");

        Assert.Contains("初始化失败", vm.Diagnostics);
        Assert.Equal(nameof(MainViewModel.Diagnostics), raised);
    }

    [Fact]
    public void SetDiagnostics_NullBecomesEmpty()
    {
        var vm = new MainViewModel();
        vm.SetDiagnostics(null!);
        Assert.Equal("", vm.Diagnostics);
    }

    [Fact]
    public void Diagnostics_StartsEmpty_AndIsReadOnlyForTheUi()
    {
        var vm = new MainViewModel();
        Assert.Equal("", vm.Diagnostics);

        var prop = typeof(MainViewModel).GetProperty(nameof(MainViewModel.Diagnostics));
        Assert.NotNull(prop);
        Assert.False(prop!.GetSetMethod(nonPublic: true)!.IsPublic);
    }
}
