// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.Windows;
using Wonslate.ViewModels;

namespace Wonslate;

public partial class MainWindow : Window
{
    private readonly MainViewModel _vm = new();

    public MainWindow()
    {
        InitializeComponent();
        DataContext = _vm;
    }

    private void TranslateButton_Click(object sender, RoutedEventArgs e) => _vm.Translate();

    private void ExchangeButton_Click(object sender, RoutedEventArgs e) => _vm.ExchangeLanguages();

    private void ModeComboBox_SelectionChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (_vm is null) return;
        var box = (System.Windows.Controls.ComboBox)sender;
        _vm.Mode = box.SelectedIndex == 0 ? "realtime" : "full";
    }
}
