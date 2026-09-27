# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
"""Tests for the XAML static lint rules in script/verify/build_check.py.

These two rules guard against "compiles clean, crashes on launch", which no other
layer of the suite can see: the BAML compiler accepts both patterns and only
WPF's runtime loader rejects them.

Run:  python tests/test_xaml_lint.py
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "script" / "verify"))

import build_check  # noqa: E402


def rules(text):
    return [(rule, line) for line, rule, _detail in build_check.lint_xaml(text)]


class XamlLintRules(unittest.TestCase):
    def test_r1_flags_dynamic_resource_inside_binding(self):
        text = '<TextBlock Text="{Binding Label, FallbackValue={DynamicResource Fb}}" />'
        self.assertIn("R1", [r for r, _ in rules(text)])

    def test_r1_ignores_plain_dynamic_resource(self):
        text = '<Border Background="{DynamicResource CardBrush}" ' \
               'Tag="{Binding Title, Mode=OneWay}" />'
        self.assertEqual([], rules(text))

    def test_r2_flags_run_text_binding_without_mode(self):
        # Run.Text defaults to TwoWay; a read-only source throws in InitializeComponent.
        text = '<TextBlock><Run Text="{Binding EngineVersion}" /></TextBlock>'
        self.assertIn("R2", [r for r, _ in rules(text)])

    def test_r2_accepts_explicit_oneway(self):
        text = '<TextBlock><Run Text="{Binding EngineVersion, Mode=OneWay}" /></TextBlock>'
        self.assertEqual([], rules(text))

    def test_literal_run_text_is_not_a_finding(self):
        text = '<TextBlock><Run Text="static text" /></TextBlock>'
        self.assertEqual([], rules(text))

    def test_reports_line_numbers(self):
        text = '<Grid>\n  <TextBlock>\n    <Run Text="{Binding X}" />\n  </TextBlock>\n</Grid>'
        findings = build_check.lint_xaml(text)
        self.assertEqual(1, len(findings))
        self.assertEqual(3, findings[0][0])


class RepositoryIsClean(unittest.TestCase):
    def test_shipped_xaml_has_no_findings(self):
        files = build_check.iter_xaml_files()
        self.assertTrue(files, "no XAML discovered: check the Wonslate.UI path")
        dirty = {str(p.relative_to(build_check.ROOT)): rules(
            p.read_text(encoding="utf-8", errors="replace")) for p in files}
        dirty = {k: v for k, v in dirty.items() if v}
        self.assertEqual({}, dirty, "XAML lint findings in the tree")

    def test_skip_dirs_are_excluded(self):
        base = pathlib.Path(build_check.ROOT)
        self.assertTrue(build_check.skip_path(base / "Wonslate.UI" / "obj" / "Debug" / "x.xaml"))
        self.assertFalse(build_check.skip_path(base / "Wonslate.UI" / "MainWindow.xaml"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
