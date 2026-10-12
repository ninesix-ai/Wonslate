// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Threading;
using System.Threading.Tasks;
using Wonslate.Interop;
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

    // ---- D49 / D35: a save states a lock opinion, and the messages say what happened ----

    [Fact]
    public void BuildGlossaryEntryJson_CarriesTheLockOpinionTheUserAskedFor()
    {
        // "Editing a term means locking it" only holds if the payload says so: the engine
        // treats an absent key as "leave the lock alone", so a dropped key is not a false
        // promise, it is no opinion at all -- and the row would stay open to distillation.
        var locked = MainViewModel.BuildGlossaryEntryJson(
            "字幕", "zh", "subtitle", "en", "", true);
        Assert.Contains("\"user_locked\":true", locked);

        var unlocked = MainViewModel.BuildGlossaryEntryJson(
            "字幕", "zh", "subtitle", "en", "", false);
        Assert.Contains("\"user_locked\":false", unlocked);
    }

    [Fact]
    public void BuildGlossaryLockJson_KeepsTheRowAndFlipsOnlyTheLock()
    {
        // The list can hold domain-scoped rows; re-saving must address that very row, not
        // silently create a generic twin next to it.
        var row = new GlossaryEntryDto("音色", "zh", "speaker timbre", "en", 1.0f, 7, "av", "seed:av", null);

        var json = MainViewModel.BuildGlossaryLockJson(row, true);

        Assert.Contains("\"domain\":\"av\"", json);
        Assert.Contains("\"target_term\":\"speaker timbre\"", json);
        Assert.Contains("\"frequency\":7", json);
        Assert.Contains("\"user_locked\":true", json);
    }

    [Fact]
    public void GlossaryDeleteMessage_DoesNotClaimADeletionThatDidNotHappen()
    {
        // Measured: deleting a domain row removed nothing and still answered ok, so the UI
        // reported "deleted" for a row that stayed on screen.
        var row = new GlossaryEntryDto("音色", "zh", "speaker timbre", "en", 1.0f, 1, "av", "seed:av", null);
        var notRemoved = new AckDto(true, null, null, null, false);

        var message = MainViewModel.GlossaryDeleteMessage(notRemoved, row);

        Assert.DoesNotContain("已删除", message);
        Assert.Contains("av", message);
        // The guard half: a delete that did remove something must still read as a deletion.
        Assert.Equal("术语已删除", MainViewModel.GlossaryDeleteMessage(new AckDto(true, null, null, null, true), row));
    }

    [Fact]
    public void GlossaryDeleteKey_CarriesTheSelectedRowsOwnDomain()
    {
        // The regression this pins is invisible to the compiler: dropping the last argument at
        // the call site still builds, and the delete then lands on the generic row of the same
        // term -- which is exactly the D50 bug, just moved from the engine into the client.
        var domainRow = new GlossaryEntryDto("音色", "zh", "speaker timbre", "en", 1.0f, 1, "av", "seed:av", null);

        Assert.Equal(new[] { "音色", "zh", "en", "av" }, MainViewModel.GlossaryDeleteKey(domainRow));

        var genericRow = new GlossaryEntryDto("音色", "zh", "speaker timbre", "en", 1.0f, 1, "", "manual", null);
        Assert.Equal(new[] { "音色", "zh", "en", "" }, MainViewModel.GlossaryDeleteKey(genericRow));
    }

    [Fact]
    public void GlossaryDeleteMessage_NamesTheRowItCouldNotFind()
    {
        // D50 closed the "cannot aim at a domain row" gap, so a refusal now means "that row
        // is not in the store" -- and the message has to say WHICH row it tried, because a
        // term can exist twice (generic and domain) and the two are different rows.
        var domainRow = new GlossaryEntryDto("音色", "zh", "speaker timbre", "en", 1.0f, 1, "av", "seed:av", null);
        var genericRow = new GlossaryEntryDto("音色", "zh", "speaker timbre", "en", 1.0f, 1, "", "manual", null);
        var notRemoved = new AckDto(true, null, null, null, false);

        var dm = MainViewModel.GlossaryDeleteMessage(notRemoved, domainRow);
        Assert.Contains("av", dm);
        Assert.Contains("领域行", dm);
        Assert.DoesNotContain("已删除", dm);

        var gm = MainViewModel.GlossaryDeleteMessage(notRemoved, genericRow);
        Assert.Contains("通用行", gm);
        Assert.DoesNotContain("领域行", gm);
    }

    [Fact]
    public void GlossarySaveMessage_ReportsTheLockTheEngineSet()
    {
        // The ack echoes the row's lock state, so the status line tells the user whether
        // distillation will respect this term from now on.
        Assert.Equal("术语已保存并锁定", MainViewModel.GlossarySaveMessage(new AckDto(true, null, null, true, null)));
        Assert.Equal("术语已保存", MainViewModel.GlossarySaveMessage(new AckDto(true, null, null, false, null)));
    }

    [Fact]
    public void GlossaryLockMessage_OnlyPromisesWhatTheEngineConfirmed()
    {
        // The lock is the promise the user is buying, so the line must come from the echo:
        // an ack that says nothing about the lock cannot be rendered as "locked now".
        Assert.Contains("已锁定", MainViewModel.GlossaryLockMessage(new AckDto(true, null, null, true, null), true));
        Assert.Contains("已解锁", MainViewModel.GlossaryLockMessage(new AckDto(true, null, null, false, null), false));
        Assert.DoesNotContain("不会再改它", MainViewModel.GlossaryLockMessage(new AckDto(true, null, null), true));
        Assert.Contains("未回报", MainViewModel.GlossaryLockMessage(new AckDto(true, null, null), true));
        Assert.Contains("失败", MainViewModel.GlossaryLockMessage(new AckDto(false, "TM_ERROR", "not initialized"), true));
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
        var vm = new MainViewModel { Input = "hello", TranslateTimeout = TimeSpan.FromMilliseconds(400), TranslateRetries = 1 };
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
        var vm = new MainViewModel { Input = "hello", TranslateTimeout = TimeSpan.FromMilliseconds(400), TranslateRetries = 1 };
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

    // ---- Shipped defaults for the outermost timeout (audit: four layers, .NET is first) ----

    [Fact]
    public void DefaultTranslateTimeout_CoversTheRustWorstCase()
    {
        // The Rust sidecar client can legitimately take up to `busy_retry (12 s) +
        // *_SIDECAR_TIMEOUT_MS (30 s)` = 42 s while it waits out a `429` budget, and a measured
        // page-scale madlad decode is ~25 s (`docs/evidence/queue-wait-madlad.json`). The UI
        // timeout shipped at 20 s and abandoned both mid-flight -- then the retry re-issued a
        // whole decode while the abandoned one still ran (defect D30 item 2). The default is now
        // a backstop above the Rust worst case, so the UI does not give up before the layer that
        // is doing the right thing has finished.
        Assert.Equal(TimeSpan.FromSeconds(45), new MainViewModel().TranslateTimeout);
    }

    [Fact]
    public void DefaultTranslateRetries_IsZero_NoOverlappingRetry()
    {
        // Retry-on-timeout re-dials a full native call while the abandoned one is still decoding;
        // with the gate OFF by default that second decode overlaps the first and slows the work
        // still wanted (measured 2.5x). The mechanism stays configurable (the two retry tests
        // above pin `TranslateRetries = 1`), but the shipped default no longer amplifies a slow
        // engine on its own.
        Assert.Equal(0, new MainViewModel().TranslateRetries);
    }

    [Fact]
    public async Task TranslateAsync_TimeoutByDefault_SingleAttemptNoRetry()
    {
        var vm = new MainViewModel { Input = "hello", TranslateTimeout = TimeSpan.FromMilliseconds(400) };
        // deliberately NOT setting TranslateRetries: this asserts the shipped default.
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
            await started.Task;
            await task;
            Assert.Equal(1, calls);              // no retry at the new default
            Assert.Equal("超时", vm.EngineLabel);
            Assert.Contains("超时", vm.Output);
            Assert.False(vm.IsBusy);
        }
        finally
        {
            release.Set();
        }
    }

    // ---- The outermost timeout is env-overridable so an operator can align it with the queue ----

    [Fact]
    public void ResolveTranslateTimeout_ReadsTheEnvAndHonorsThePrefixChain()
    {
        // Absent -> shipped default; the brand spelling is honored; the legacy LT_ prefix wins
        // when both are present (the same precedence chain as the rest of the config layer).
        Assert.Equal(TimeSpan.FromSeconds(45), MainViewModel.ResolveTranslateTimeout(_ => null));
        Assert.Equal(TimeSpan.FromSeconds(60),
            MainViewModel.ResolveTranslateTimeout(k =>
                k == "WONSLATE_UI_TRANSLATE_TIMEOUT_MS" ? "60000" : null));
        Assert.Equal(TimeSpan.FromSeconds(90),
            MainViewModel.ResolveTranslateTimeout(k => k switch
            {
                "LT_UI_TRANSLATE_TIMEOUT_MS" => "90000",
                "WONSLATE_UI_TRANSLATE_TIMEOUT_MS" => "60000",
                _ => null,
            }));
    }

    [Fact]
    public void ResolveTranslateTimeout_GarbageOrNonPositive_FallsBackToDefault()
    {
        // A typo must not silently disable the timeout: `0` or negative would mean "never time
        // out" (the native call has no other bound), and unparsable text is not a duration. Both
        // fall back to the shipped default rather than to an unbounded wait.
        foreach (var bad in new[] { "not-a-number", "", "   ", "0", "-1" })
        {
            var resolved = MainViewModel.ResolveTranslateTimeout(k =>
                k == "LT_UI_TRANSLATE_TIMEOUT_MS" ? bad : null);
            Assert.Equal(TimeSpan.FromSeconds(45), resolved);
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

    // ---- which engine served is reported out, so only that endpoint warms ----

    [Fact]
    public void Translate_ReportsTheEngineThatActuallyServed()
    {
        // The shell needs a fact, not a guess: the response names the engine, so warming
        // follows the routing decision instead of duplicating it in the UI.
        var seen = new List<string>();
        var vm = new MainViewModel { Input = "hello", OnEngineUsed = seen.Add };
        vm.InvokeTranslate = _ => OkJson;

        vm.Translate();

        Assert.Equal(new[] { "demo" }, seen);
    }

    [Fact]
    public void Translate_ReportsASidecarEngineByItsOwnName()
    {
        var seen = new List<string>();
        var vm = new MainViewModel { Input = "hello", OnEngineUsed = seen.Add };
        vm.InvokeTranslate = _ => """
            {"ok":true,"engine":"madlad","source_lang":"en","target_lang":"zh",
             "input":"hello","output":"你好","source":"local","latency_ms":4100,"confidence":0.86}
            """;

        vm.Translate();

        Assert.Equal(new[] { "madlad" }, seen);
    }

    [Fact]
    public void Translate_WhenTheEngineFailed_ReportsNothingToWarm()
    {
        // A failed request proved no engine is resident; warming on it would knock on the
        // port of whatever was blamed for the failure.
        var seen = new List<string>();
        var vm = new MainViewModel { Input = "hello", OnEngineUsed = seen.Add };
        vm.InvokeTranslate = _ => """
            {"ok":false,"engine":"","source_lang":"en","target_lang":"zh",
             "input":"hello","output":"","source":"","error":"NO_RESULT","message":"no engine could translate"}
            """;

        vm.Translate();

        Assert.Empty(seen);
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
