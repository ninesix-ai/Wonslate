// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.ComponentModel;
using System.IO;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using Wonslate.Interop;

namespace Wonslate.ViewModels;

/// <summary>One entry of the quality/cost choice shown in the settings page.</summary>
public sealed record QualityOption(string Value, string Label, string Explanation);

/// <summary>
/// The settings page (D15): lets the user choose between quality and cost, and shows the
/// value that is <em>actually</em> in force rather than the one just saved.
///
/// Two deliberate constraints:
/// * Applying is a restart. The engine reads its configuration once at start-up, so the
///   page says so instead of pretending the change took effect.
/// * The engine's effective routing is shown read from the engine itself, including any
///   environment variable that overrides the user's choice. Without that, "I set it and
///   nothing changed" would be indistinguishable from a bug.
/// </summary>
public sealed class SettingsViewModel : INotifyPropertyChanged
{
    /// <summary>Empty while the user has not chosen anything yet.</summary>
    private const string Unset = "";

    private readonly Func<string> _readEngineConfig;
    private readonly Func<string> _settingsPath;
    private readonly Func<string, bool> _writeSettings;

    private string _preference = Unset;
    private string _savedPreference = Unset;
    private string _effective = "";
    private string _status = "";
    private string? _engineSettingsPath;

    public SettingsViewModel(
        Func<string>? readEngineConfig = null,
        Func<string>? settingsPath = null,
        Func<string, bool>? writeSettings = null)
    {
        _readEngineConfig = readEngineConfig ?? EngineNative.ConfigJson;
        _settingsPath = settingsPath ?? (() => _engineSettingsPath ?? DefaultSettingsPath());
        _writeSettings = writeSettings ?? (Func<string, bool>)(json => WriteSettingsFile(_settingsPath(), json));
        // Deliberately NOT loaded here: this runs while the ViewModel is being built, and
        // the engine may not be up yet. The window calls Reload() once it is.
    }

    public IReadOnlyList<QualityOption> Options { get; } = new[]
    {
        new QualityOption("cost_first", "省成本优先",
            "本地引擎能翻就用本地，命中缓存直接用。最快、最省；缓存的译文可能来自本地引擎（质量 0.88）。"),
        new QualityOption("quality_first", "质量优先",
            "每次都交给 AI 引擎精译，且不直接使用本地引擎写入的缓存（会重新精译一次，此后使用 AI 结果）。更慢、更贵。"
            + "注意：没有可用的 AI 引擎时缓存会整体失效，同一句话每次都要重新翻译。"),
    };

    /// <summary>The chosen option, bound to the list's SelectedItem.
    ///
    /// Bound to the option object rather than to a string value through
    /// SelectedValue/SelectedValuePath on purpose: with a path, one wrong property name
    /// ("Label" instead of "Value") makes the app persist a display string, which the
    /// engine then ignores in silence - the exact failure this page exists to prevent.
    /// Binding the object removes that possibility.
    /// </summary>
    public QualityOption? Selected
    {
        get => Options.FirstOrDefault(o => o.Value == _preference);
        set
        {
            var value0 = value?.Value ?? Unset;
            if (_preference == value0) return;
            _preference = value0;
            OnPropertyChanged();
            OnPropertyChanged(nameof(Preference));
            OnPropertyChanged(nameof(HasUnsavedChange));
        }
    }

    /// <summary>The user's choice as the engine's canonical string; saved on <see cref="Save"/>.</summary>
    public string Preference
    {
        get => _preference;
        set
        {
            if (_preference == value) return;
            _preference = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(Selected));
            OnPropertyChanged(nameof(HasUnsavedChange));
        }
    }

    /// <summary>What the engine reports as effective, verbatim from the engine.</summary>
    public string Effective
    {
        get => _effective;
        private set { _effective = value; OnPropertyChanged(); }
    }

    public string Status
    {
        get => _status;
        private set { _status = value; OnPropertyChanged(); }
    }

    public string SettingsPath => _settingsPath();

    public bool HasUnsavedChange => _preference != _savedPreference;

    /// <summary>Re-read the effective routing from the engine.</summary>
    public void Reload()
    {
        try
        {
            var raw = _readEngineConfig();
            // The engine owns the path (it honours LT_DATA_DIR / WONSLATE_DATA_DIR), so
            // ask it rather than recomputing the rule here: two copies of that rule drift
            // and the file the app writes stops being the file the engine reads.
            _engineSettingsPath = ExtractSettingsPath(raw);
            Effective = DescribeEffective(raw);
            var saved = ReadSavedPreference();
            _savedPreference = saved ?? Unset;
            if (saved is null)
            {
                // No choice was ever saved. Do NOT preselect one: the deployment default
                // (upgrade_policy=always with no serving floor) is neither option, so
                // showing a selection would both misreport the current state and turn a
                // later save into a silent behaviour change.
                Preference = Unset;
                Status = "还没有保存过设置，当前使用部署默认值——它既不是上面两项，具体见下方「实际生效」。";
            }
            else
            {
                Status = $"已保存的偏好：{Label(saved)}。改动需重启软件后生效。";
            }
        }
        catch (Exception ex)
        {
            Effective = "";
            Status = "读取引擎配置失败：" + ex.Message;
        }
    }

    /// <summary>Persist the choice to the user settings file.</summary>
    public void Save()
    {
        // Unreachable through the UI (the button is disabled without a change), but a
        // settings file with an empty preference would be a silent no-op later.
        if (_preference is not ("quality_first" or "cost_first"))
        {
            Status = "请先选择一项再保存。";
            return;
        }
        var json = new JsonObject { ["quality_preference"] = _preference }.ToJsonString(
            new JsonSerializerOptions { WriteIndented = true });
        if (!_writeSettings(json))
        {
            Status = "保存失败，请检查设置目录是否可写：" + SettingsPath;
            return;
        }
        _savedPreference = _preference;
        OnPropertyChanged(nameof(HasUnsavedChange));
        Status = "已保存。**需重启软件后生效**（引擎在启动时读取配置）。";
    }

    private static string Label(string value) => value switch
    {
        "quality_first" => "质量优先",
        "cost_first" => "省成本优先",
        _ => "（未设置）",
    };

    /// <summary>
    /// Turn the engine's config snapshot into a short human summary. Kept deliberately
    /// factual: it reports the effective policy and floor, and names any environment
    /// variable that is overriding the user, because that is the usual answer to
    /// "why did my setting not apply".
    /// </summary>
    internal static string DescribeEffective(string configJson)
    {
        try
        {
            using var doc = JsonDocument.Parse(configJson);
            var root = doc.RootElement;
            if (!root.TryGetProperty("routing", out var routing))
                return "(引擎未返回路由配置)";

            var parts = new List<string>();
            if (routing.TryGetProperty("full", out var full))
            {
                var policy = full.TryGetProperty("upgrade_policy", out var p) ? p.GetString() : "?";
                var floor = full.TryGetProperty("tm_quality_floor", out var f) ? f.GetDouble() : 0;
                parts.Add($"精译档：升级策略 {policy}，缓存质量下限 {floor:0.00}");
            }
            if (routing.TryGetProperty("realtime", out var realtime))
            {
                var engine = realtime.TryGetProperty("local_engine", out var e) ? e.GetString() : "?";
                parts.Add($"实时档：本地引擎 {engine}，不升级");
            }
            if (root.TryGetProperty("settings_file", out var sf))
                parts.Add($"用户设置文件：{sf.GetString()}");

            if (root.TryGetProperty("env_pinned", out var pinned) && pinned.GetArrayLength() > 0)
            {
                var names = pinned.EnumerateArray().Select(x => x.GetString()).Where(x => x is not null);
                parts.Add("⚠️ 以下环境变量正在覆盖用户设置：" + string.Join("、", names));
            }
            return string.Join(Environment.NewLine, parts);
        }
        catch (JsonException)
        {
            return "(引擎返回的配置无法解析)";
        }
    }

    /// <summary>The saved preference, or null when no usable value was written yet.</summary>
    private string? ReadSavedPreference()
    {
        try
        {
            var path = _settingsPath();
            if (!File.Exists(path)) return null;
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            if (doc.RootElement.TryGetProperty("quality_preference", out var p))
            {
                var value = p.GetString();
                // Anything else is a value the engine would ignore, so it must not be
                // presented as an in-force choice; fall through to the unset state.
                if (value is "quality_first" or "cost_first")
                {
                    Preference = value;
                    return value;
                }
            }
            return null;
        }
        catch (Exception)
        {
            return null;   // unreadable settings must not stop the page from opening
        }
    }

    /// <summary>The settings path the engine reported, or null when it did not say.</summary>
    internal static string? ExtractSettingsPath(string configJson)
    {
        try
        {
            using var doc = JsonDocument.Parse(configJson);
            if (doc.RootElement.TryGetProperty("settings_file", out var p)
                && p.GetString() is { Length: > 0 } path)
            {
                return path;
            }
            return null;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static string DefaultSettingsPath() =>
        Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Wonslate", "config", "settings.json");

    private static bool WriteSettingsFile(string path, string json)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, json);
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void OnPropertyChanged([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}
