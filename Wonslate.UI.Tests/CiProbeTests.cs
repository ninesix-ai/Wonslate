// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
//
// TEMPORARY CI PROBE - DO NOT MERGE.
// Exists only to prove that a failing .NET test reaches the published report:
//   - the .NET WPF tests check run must conclude failure,
//   - the failing case must appear as a check annotation,
//   - the pre-commit hook behaviour is observed separately (see the commit note).
// This file is deleted in the same branch before it is closed.
using Xunit;

namespace Wonslate.UI.Tests;

public class CiProbeTests
{
    [Fact]
    public void Probe_Passes_SoTheReportShowsMixedResults()
    {
        Assert.Equal(2, 1 + 1);
    }

    [Fact]
    public void Probe_FailsOnPurpose_SoAnnotationsGetPublished()
    {
        Assert.Equal("expected-but-not-produced", "probe");
    }
}
