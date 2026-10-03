// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Text.Json;
using Wonslate.ViewModels;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// Settings page tests (D15). Every test injects the engine reader and the settings writer,
/// so nothing here touches the FFI or the user's real settings file.
///
/// The properties that make this page honest are what these tests pin down:
///  - the value saved is the engine's canonical string, never the display label;
///  - nothing is preselected before a choice was ever saved, because the deployment
///    default is neither option and a false preselection would turn a later save into a
///    silent behaviour change;
///  - what is shown as "effective" comes from the engine, including any environment
///    variable overriding the user.
/// </summary>
public class SettingsViewModelTests
{
    private const string EngineConfigJson = """
        {
          "routing": {
            "common_pairs": [["en","zh"]],
            "realtime": {"local_engine":"argos","upgrade_engine":null,"upgrade_threshold":0.0,
                         "upgrade_policy":"never"},
            "full": {"local_engine":"argos","upgrade_engine":"ollama","upgrade_threshold":0.7,
                     "upgrade_policy":"always","tm_quality_floor":0.95},
            "rare_pair": {"local_engine":"demo","upgrade_engine":null,"upgrade_threshold":0.0,
                          "upgrade_policy":"never"}
          },
          "env_pinned": [],
          "routes_file": "C:/app/routes.json",
          "settings_file": "C:/data/config/settings.json"
        }
        """;

    /// <summary>A settings page wired to fakes; nothing reaches disk or the engine.
    /// Pass <paramref name="settingsPath"/> = null to exercise the engine-reported path.</summary>
    private static SettingsViewModel Make(
        string engineJson = EngineConfigJson,
        string? settingsPath = "C:/data/config/settings.json",
        List<string>? written = null,
        bool writeOk = true)
    {
        var sink = written ?? new List<string>();
        return new SettingsViewModel(
            readEngineConfig: () => engineJson,
            settingsPath: settingsPath is null ? null : () => settingsPath,
            writeSettings: json => { sink.Add(json); return writeOk; });
    }

    // ---- Initial state and the option list ---------------------------------

    [Fact]
    public void NewViewModel_HasNothingSelected()
    {
        // Before Reload() the page knows nothing about what is in force, so it must not
        // imply a choice. After Reload() with no saved file, the same holds.
        var vm = Make();
        Assert.Equal("", vm.Preference);
        Assert.Null(vm.Selected);
        Assert.False(vm.HasUnsavedChange);
    }

    [Fact]
    public void Options_OfferExactlyQualityAndCost()
    {
        var vm = Make();
        Assert.Equal(2, vm.Options.Count);
        Assert.Contains(vm.Options, o => o.Value == "cost_first");
        Assert.Contains(vm.Options, o => o.Value == "quality_first");
        // Every option must explain its cost, or the choice is a coin flip for the user.
        Assert.All(vm.Options, o => Assert.False(string.IsNullOrWhiteSpace(o.Explanation)));
    }

    [Fact]
    public void Options_ValueIsTheCanonicalEngineString_NeverTheLabel()
    {
        // These are the only two strings config.rs accepts. If the UI ever persisted the
        // label instead, the engine would silently ignore the setting - the exact failure
        // the "show the effective value" design exists to avoid.
        var vm = Make();
        foreach (var o in vm.Options)
        {
            Assert.True(o.Value is "quality_first" or "cost_first", $"unexpected value {o.Value}");
            Assert.NotEqual(o.Label, o.Value);
        }
    }

    [Fact]
    public void SelectingAnOption_SetsItsCanonicalValue()
    {
        var vm = Make();
        var quality = vm.Options[1];

        vm.Selected = quality;

        Assert.Equal("quality_first", vm.Preference);
        Assert.True(vm.HasUnsavedChange);
    }

    [Fact]
    public void Selected_IsDerivedFromThePreference()
    {
        // The two must never disagree: the list shows Selected, the file gets Preference.
        var vm = Make();
        vm.Preference = "quality_first";
        Assert.Equal("quality_first", vm.Selected?.Value);

        vm.Preference = "cost_first";
        Assert.Equal("cost_first", vm.Selected?.Value);

        vm.Preference = "";
        Assert.Null(vm.Selected);
    }

    [Fact]
    public void Preference_SetterRaisesChangeNotifications()
    {
        var vm = Make();
        var raised = new List<string?>();
        vm.PropertyChanged += (_, e) => raised.Add(e.PropertyName);

        vm.Preference = "quality_first";

        Assert.Contains(nameof(SettingsViewModel.Preference), raised);
        Assert.Contains(nameof(SettingsViewModel.Selected), raised);
        Assert.Contains(nameof(SettingsViewModel.HasUnsavedChange), raised);
        Assert.True(vm.HasUnsavedChange);
    }

    [Fact]
    public void Selected_SetterRaisesChangeNotifications()
    {
        var vm = Make();
        var raised = new List<string?>();
        vm.PropertyChanged += (_, e) => raised.Add(e.PropertyName);

        vm.Selected = vm.Options[1];

        Assert.Contains(nameof(SettingsViewModel.Selected), raised);
        Assert.Contains(nameof(SettingsViewModel.Preference), raised);
    }

    [Fact]
    public void Preference_SameValue_DoesNotNotify()
    {
        var vm = Make();
        vm.Preference = "cost_first";
        int count = 0;
        vm.PropertyChanged += (_, e) => { if (e.PropertyName == nameof(SettingsViewModel.Preference)) count++; };

        vm.Preference = "cost_first";   // already the value

        Assert.Equal(0, count);
    }

    [Fact]
    public void ImplementsINotifyPropertyChanged()
    {
        Assert.IsAssignableFrom<INotifyPropertyChanged>(Make());
    }

    // ---- Save ---------------------------------------------------------------

    [Fact]
    public void Save_WritesTheCanonicalValue_AndSaysRestartIsRequired()
    {
        var written = new List<string>();
        var vm = Make(written: written);
        vm.Selected = vm.Options[1];   // quality_first

        vm.Save();

        Assert.Single(written);
        Assert.Contains("\"quality_preference\": \"quality_first\"", written[0]);
        Assert.DoesNotContain("质量优先", written[0]);
        Assert.False(vm.HasUnsavedChange);
        Assert.Contains("已保存", vm.Status);
        // Applying is a restart: the page must say so rather than imply it took effect.
        Assert.Contains("重启", vm.Status);
    }

    [Fact]
    public void Save_StopsReportingAnUnsavedChange()
    {
        var vm = Make();
        vm.Preference = "quality_first";
        Assert.True(vm.HasUnsavedChange);

        vm.Save();

        Assert.False(vm.HasUnsavedChange);   // the save button disables itself via this
    }

    [Fact]
    public void Save_WithNothingSelected_RefusesInsteadOfWritingAnEmptyPreference()
    {
        var written = new List<string>();
        var vm = Make(written: written);

        vm.Save();

        Assert.Empty(written);                       // the engine would ignore an empty value
        Assert.Contains("请先选择", vm.Status);
    }

    [Fact]
    public void Save_WhenWriteFails_ReportsFailure_AndKeepsTheChangePending()
    {
        var vm = Make(writeOk: false);
        vm.Preference = "quality_first";

        vm.Save();

        Assert.Contains("保存失败", vm.Status);
        Assert.Contains(vm.SettingsPath, vm.Status);      // names the directory to check
        Assert.True(vm.HasUnsavedChange);                 // so the user can retry
    }

    [Fact]
    public void Save_WritesJsonThatCanBeReadBack()
    {
        var written = new List<string>();
        var vm = Make(written: written);
        vm.Preference = "quality_first";
        vm.Save();

        using var doc = JsonDocument.Parse(written[0]);
        Assert.Equal("quality_first", doc.RootElement.GetProperty("quality_preference").GetString());
    }

    // ---- Reload: effective routing from the engine ------------------------------

    [Fact]
    public void Reload_ShowsEffectivePolicyAndFloor()
    {
        var vm = Make();

        vm.Reload();

        Assert.Contains("精译档", vm.Effective);
        Assert.Contains("always", vm.Effective);
        Assert.Contains("0.95", vm.Effective);   // the AI-grade serving floor
        Assert.Contains("实时档", vm.Effective);
    }

    [Fact]
    public void Reload_WarnsWhenAnEnvironmentVariableOverridesTheUser()
    {
        // The usual answer to "I changed it and nothing happened". Without this warning the
        // page would look broken instead of overridden.
        const string pinned = """
            {"routing":{"full":{"upgrade_policy":"low_confidence","tm_quality_floor":0.0}},
             "env_pinned":["LT_QUALITY_PREFERENCE"],"settings_file":"C:/data/config/settings.json"}
            """;
        var vm = Make(engineJson: pinned);

        vm.Reload();

        Assert.Contains("LT_QUALITY_PREFERENCE", vm.Effective);
        Assert.Contains("覆盖", vm.Effective);
    }

    [Fact]
    public void Reload_DoesNotWarnWhenNothingIsOverridden()
    {
        var vm = Make();

        vm.Reload();

        Assert.DoesNotContain("⚠", vm.Effective);
    }

    [Fact]
    public void Reload_ReportsTheSettingsPathTheEngineUses()
    {
        // Not the app's own guess: the engine honours LT_DATA_DIR, so a locally recomputed
        // path would drift and the file the app writes would stop being the one it reads.
        var vm = Make(engineJson: EngineConfigJson.Replace("C:/data", "D:/elsewhere"),
            settingsPath: null);

        vm.Reload();

        Assert.Equal("D:/elsewhere/config/settings.json", vm.SettingsPath);
    }

    [Fact]
    public void Reload_WhenEngineCallThrows_ShowsTheError_AndDoesNotThrow()
    {
        var vm = new SettingsViewModel(
            readEngineConfig: () => throw new InvalidOperationException("native library missing"),
            settingsPath: () => "C:/data/config/settings.json",
            writeSettings: _ => true);

        vm.Reload();   // must not propagate: the window calls this during load

        Assert.Contains("native library missing", vm.Status);
        Assert.Equal("", vm.Effective);
    }

    [Fact]
    public void Reload_WhenEngineReturnsGarbage_SaysSoInsteadOfCrashing()
    {
        var vm = Make(engineJson: "not json at all");

        vm.Reload();

        Assert.Contains("无法解析", vm.Effective);
    }

    // ---- Reload: reading back what was saved --------------------------------------

    [Fact]
    public void Reload_AdoptsTheSavedPreferenceFromDisk()
    {
        var dir = Directory.CreateTempSubdirectory("wonslate-settings-test-");
        var path = Path.Combine(dir.FullName, "settings.json");
        File.WriteAllText(path, """{"quality_preference": "quality_first"}""");
        try
        {
            var vm = Make(settingsPath: path);

            vm.Reload();

            Assert.Equal("quality_first", vm.Preference);
            Assert.Equal("quality_first", vm.Selected?.Value);   // list shows the saved choice
            Assert.False(vm.HasUnsavedChange);          // it matches what is on disk
            Assert.Contains("质量优先", vm.Status);
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    [Fact]
    public void Reload_WithNoSettingsFile_LeavesNothingSelected_AndSaysDefaultsAreInUse()
    {
        var dir = Directory.CreateTempSubdirectory("wonslate-settings-test-");
        try
        {
            var vm = Make(settingsPath: Path.Combine(dir.FullName, "absent.json"));

            vm.Reload();

            // Not "cost_first": the deployment default is `always` with no serving floor,
            // which is neither option. Preselecting one would misreport the state and make
            // a later save a silent behaviour change.
            Assert.Equal("", vm.Preference);
            Assert.Null(vm.Selected);
            Assert.Contains("部署默认值", vm.Status);
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    [Fact]
    public void Reload_IgnoresAnUnknownPreferenceValue()
    {
        var dir = Directory.CreateTempSubdirectory("wonslate-settings-test-");
        var path = Path.Combine(dir.FullName, "settings.json");
        File.WriteAllText(path, """{"quality_preference": "whatever"}""");
        try
        {
            var vm = Make(settingsPath: path);

            vm.Reload();

            // config.rs would reject this, so presenting it as the in-force choice would lie.
            Assert.Equal("", vm.Preference);
            Assert.Null(vm.Selected);
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    [Fact]
    public void Reload_WithCorruptSettingsFile_KeepsThePageOpen()
    {
        var dir = Directory.CreateTempSubdirectory("wonslate-settings-test-");
        var path = Path.Combine(dir.FullName, "settings.json");
        File.WriteAllText(path, "{ this is not json");
        try
        {
            var vm = Make(settingsPath: path);

            vm.Reload();

            Assert.Equal("", vm.Preference);
            Assert.Contains("精译档", vm.Effective);       // the engine half still rendered
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    [Fact]
    public void Reload_SavedPreferenceDrift_IsReportedAsUnsaved()
    {
        var dir = Directory.CreateTempSubdirectory("wonslate-settings-test-");
        var path = Path.Combine(dir.FullName, "settings.json");
        File.WriteAllText(path, """{"quality_preference": "cost_first"}""");
        try
        {
            var vm = Make(settingsPath: path);
            vm.Reload();
            vm.Selected = vm.Options[1];   // switch to quality_first

            Assert.True(vm.HasUnsavedChange);
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    // ---- Static parsing helpers -------------------------------------------------

    [Fact]
    public void ExtractSettingsPath_ReadsTheEngineReportedPath()
    {
        Assert.Equal("C:/data/config/settings.json",
            SettingsViewModel.ExtractSettingsPath(EngineConfigJson));
    }

    [Theory]
    [InlineData("{}")]
    [InlineData("""{"settings_file": null}""")]
    [InlineData("""{"settings_file": ""}""")]
    [InlineData("not json")]
    public void ExtractSettingsPath_ReturnsNullWhenTheEngineDidNotSay(string json)
    {
        // null means "fall back to the app's own path", so this must never throw.
        Assert.Null(SettingsViewModel.ExtractSettingsPath(json));
    }

    [Fact]
    public void DescribeEffective_HandlesAMissingRoutingBlock()
    {
        Assert.Equal("(引擎未返回路由配置)", SettingsViewModel.DescribeEffective("{}"));
    }

    [Fact]
    public void DescribeEffective_HandlesUnparsableInput()
    {
        Assert.Equal("(引擎返回的配置无法解析)", SettingsViewModel.DescribeEffective("<html>"));
    }

    [Fact]
    public void DescribeEffective_OmitsTheFloorWhenTheEngineDoesNotReportOne()
    {
        // The realtime rule has no serving floor; the summary must not invent 0.00 for it.
        var text = SettingsViewModel.DescribeEffective("""
            {"routing":{"realtime":{"local_engine":"argos"}}}
            """);

        Assert.Contains("argos", text);
        Assert.Contains("不升级", text);
    }

    [Fact]
    public void SettingsPath_IsExposedToTheUi()
    {
        // The page shows the path so "where did my setting go" is answerable without a debugger.
        var vm = Make(settingsPath: "E:/custom/settings.json");
        Assert.Equal("E:/custom/settings.json", vm.SettingsPath);
    }
}
