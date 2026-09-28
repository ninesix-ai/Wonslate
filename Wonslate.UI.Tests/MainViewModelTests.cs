// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System.ComponentModel;
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
}
