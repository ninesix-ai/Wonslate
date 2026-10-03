// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.Windows;
using Wonslate.ViewModels;

namespace Wonslate;

public partial class MainWindow : Window
{
    private readonly MainViewModel _vm;

    /// <summary>Set once the window is shown: the engine is initialized by then, so FFI reads are safe.</summary>
    private bool _ready;

    /// <summary>The shell creates the ViewModel so it can push startup diagnostics into it.</summary>
    public MainWindow(MainViewModel viewModel)
    {
        _vm = viewModel;
        InitializeComponent();
        DataContext = _vm;
    }

    /// <summary>Load the TM tab once at startup; the tab-selection handler stays silent before this.</summary>
    private void Window_Loaded(object sender, RoutedEventArgs e)
    {
        _ready = true;
        _vm.RefreshTm();
        // The engine is up by now, so the settings page can report what is in force.
        _vm.Settings.Reload();
    }

    /// <summary>
    /// Async event handler: the engine call runs off the dispatcher thread, so a slow
    /// engine cannot freeze the window. TranslateAsync owns every failure path.
    /// </summary>
    private async void TranslateButton_Click(object sender, RoutedEventArgs e)
        => await _vm.TranslateAsync();

    private void CancelButton_Click(object sender, RoutedEventArgs e) => _vm.CancelTranslate();

    private void ExchangeButton_Click(object sender, RoutedEventArgs e) => _vm.ExchangeLanguages();

    private void ModeComboBox_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (_vm is null) return;
        var box = (System.Windows.Controls.ComboBox)sender;
        _vm.Mode = box.SelectedIndex == 0 ? "realtime" : "full";
    }

    // ---- TM / glossary management (N-07) ----------------------------------------

    private void SaveTmButton_Click(object sender, RoutedEventArgs e) => _vm.SaveCurrentToTm();

    private void RefreshTmButton_Click(object sender, RoutedEventArgs e) => _vm.RefreshTm();

    private void FlagBadTmButton_Click(object sender, RoutedEventArgs e) => _vm.FlagBadSelectedTm();

    private void SaveGlossaryButton_Click(object sender, RoutedEventArgs e) => _vm.SaveGlossaryTerm();

    private void DeleteGlossaryButton_Click(object sender, RoutedEventArgs e) => _vm.DeleteSelectedGlossaryTerm();

    private void RefreshGlossaryButton_Click(object sender, RoutedEventArgs e) => _vm.RefreshGlossary();

    // ---- Settings page (D15) ----------------------------------------------------

    private void SettingsSaveButton_Click(object sender, RoutedEventArgs e) => _vm.Settings.Save();

    private void SettingsReloadButton_Click(object sender, RoutedEventArgs e) => _vm.Settings.Reload();

    // ---- Voice panel (N-10, M4) -------------------------------------------------

    private void VoiceSelectButton_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new Microsoft.Win32.OpenFileDialog
        {
            Title = "选择一段语音（WAV，16 kHz 单声道最佳）",
            Filter = "WAV 音频 (*.wav)|*.wav|所有文件 (*.*)|*.*",
        };
        if (dialog.ShowDialog(this) == true) _vm.Voice.SetFile(dialog.FileName);
    }

    private async void VoiceRunButton_Click(object sender, RoutedEventArgs e)
        => await _vm.Voice.RunAsync(new Audio.VoiceOptions(
            _vm.SourceLang == MainViewModel.AutoDetectCode ? "zh" : _vm.SourceLang,
            _vm.TargetLang, _vm.Mode, _vm.PrivacySensitive));

    /// <summary>Playback uses System.Media.SoundPlayer, so no audio package is needed for output.</summary>
    private void VoicePlayButton_Click(object sender, RoutedEventArgs e) => _vm.Voice.PlayLast();

    /// <summary>
    /// Load the list of the tab that just became visible. Guards:
    ///  - !_ready: the selected tab is settled during InitializeComponent, before
    ///    the engine is initialized; the first load happens in Window_Loaded.
    ///  - the sender check: Selector.SelectionChanged bubbles, so a row click
    ///    inside a ListView would otherwise re-enter and refresh under the cursor.
    /// The tab is identified by Tag rather than index: adding the voice tab already
    /// shifted the indices once, and a silent off-by-one would refresh the wrong list.
    /// </summary>
    private void ManagementTab_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (!_ready) return;
        if (sender is not System.Windows.Controls.TabControl tabs) return;
        switch ((tabs.SelectedItem as System.Windows.Controls.TabItem)?.Tag as string)
        {
            case "tm": _vm.RefreshTm(); break;
            case "glossary": _vm.RefreshGlossary(); break;
            // "voice": the panel loads lazily on first use; nothing to refresh here.
        }
    }
}
