// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System.ComponentModel;
using Wonslate.ViewModels;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// MainViewModel 状态机测试。仅覆盖不触发 FFI 的路径：
///  - 属性 setter 是否正确 raise PropertyChanged
///  - 空输入 Translate() 早退（不进 FFI）
///  - 初始状态默认值（P0 演示文案）
/// 需要 P/Invoke 的翻译路径由 Python FFI 冒烟（tests/test_phase1_ffi.py）覆盖。
/// </summary>
public class MainViewModelTests
{
    // ── 初始状态 ─────────────────────────────────────────────────

    [Fact]
    public void NewViewModel_HasP0DemoInput()
    {
        var vm = new MainViewModel();
        Assert.Equal("Hello there", vm.Input);   // 演示词，走 demo 引擎可看到"你好"
        Assert.Equal("", vm.Output);
        Assert.Equal("realtime", vm.Mode);
        Assert.False(vm.PrivacySensitive);
        Assert.False(vm.IsBusy);
    }

    // ── PropertyChanged 事件契约 ────────────────────────────────

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
        // 当前实现不做相等性去重，UI 层每次赋值都通知。测试锁死该语义。
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
        // 双向绑定要求：属性必须有 public setter
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
        // 输出侧属性：VM 内部计算写入，UI 只能读，防误改
        var prop = typeof(MainViewModel).GetProperty(propName);
        Assert.NotNull(prop);
        Assert.True(prop!.CanRead);
        var setter = prop.GetSetMethod(nonPublic: true);
        Assert.NotNull(setter);
        Assert.False(setter!.IsPublic, $"{propName} setter 应为 private");
    }

    // ── Translate() 早退路径（不触发 FFI）──────────────────────

    [Fact]
    public void Translate_EmptyInput_ClearsOutput_AndSkipsFfi()
    {
        var vm = new MainViewModel { Input = "" };

        vm.Translate();   // 空输入直接早退，不进 FFI

        Assert.Equal("", vm.Output);
        Assert.False(vm.IsBusy);   // 未进入 busy 状态
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
        vm.Input = null!;   // 强制置 null 触发 ?.Trim() 兜底路径

        vm.Translate();

        Assert.Equal("", vm.Output);
    }

    // ── INotifyPropertyChanged 契约 ─────────────────────────────

    [Fact]
    public void ImplementsINotifyPropertyChanged()
    {
        // WPF 绑定的最低要求
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

    // ── 语言对解耦（①）───────────────────────────────

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
}
