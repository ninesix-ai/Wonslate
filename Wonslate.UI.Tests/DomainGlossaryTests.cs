// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio
using Wonslate.Interop;
using Xunit;

namespace Wonslate.UI.Tests;

/// <summary>
/// S11 RED-PHASE tests for the domain-aware glossary API surface.
///
/// See `docs/tasks/S11-*.md` in the outer task ledger. These assertions author the
/// *desired* contract; on the current tree they fail because:
///   - `EngineNative.GlossaryList` has no `domain` parameter (test ⑧);
///   - `MainViewModel.GlossaryDomainFilter` does not exist (test ⑨);
///   - `EngineNative.GlossaryImportPack` does not exist (test ⑩).
///
/// The failures are compile errors for the missing P/Invoke surface, which is
/// the correct RED signal for "the API needs to be added".
///
/// Do NOT ship the production code until these tests are seen failing first.
/// Run: `dotnet test --filter FullyQualifiedName~DomainGlossaryTests` (Release).
/// </summary>
public class DomainGlossaryTests
{
    /// <summary>
    /// ⑧ `EngineNative.GlossaryList(sourceLang, targetLang, domain, limit)` must
    /// exist and return a JSON array string. Test does not init the TM store
    /// (that would race with other tests in the same process), so the store
    /// returns an empty array on read -- which is still proof the P/Invoke
    /// surface resolves and marshals UTF-8 correctly across the boundary.
    /// </summary>
    [Fact]
    public void GlossaryList_AcceptsDomainAndLimit()
    {
        string json = EngineNative.GlossaryList("zh", "en", "av", 10);

        Assert.NotNull(json);
        Assert.StartsWith("[", json);
        Assert.EndsWith("]", json);
    }

    /// <summary>
    /// ⑨ MainViewModel exposes a `GlossaryDomainFilter` string that drives the
    /// glossary list reload: setting it must refresh `GlossaryEntries` through
    /// the domain-aware API. Compile fails today (property absent).
    /// </summary>
    [Fact]
    public void MainViewModel_Exposes_GlossaryDomainFilter()
    {
        // Bootstrap just enough config for the view model to talk to the engine.
        var vm = new ViewModels.MainViewModel();

        // Desired: property exists and is settable, and raising PropertyChanged
        // triggers a re-list. Compile fails today: the property is not declared.
        vm.GlossaryDomainFilter = "av";
        Assert.Equal("av", vm.GlossaryDomainFilter);
    }

    /// <summary>
    /// ⑩ FFI must expose a one-shot glossary pack importer that survives a
    /// UTF-8 payload across the boundary. The store is not initialised here, so
    /// the ack is `{"ok":false,"error":"TM_ERROR",...}` -- that is still proof
    /// the P/Invoke resolves and the FFI does not panic on a bad precondition.
    /// The Rust integration suite (domain_routing.rs ⑦) exercises the happy
    /// path with a properly initialised store.
    /// </summary>
    [Fact]
    public void GlossaryImportPack_Exists_OnEngineNative()
    {
        var packJson = """
            {"domain":"av","source_lang":"zh","target_lang":"en","version":"test-1","entries":[
              {"source_term":"语段","target_term":"speech segment","confidence":1.0}
            ]}
            """;
        string ack = EngineNative.GlossaryImportPack(packJson);

        Assert.NotNull(ack);
        Assert.StartsWith("{", ack);
        // Ack is JSON carrying an "ok" field either way; the store-refused path
        // adds an "error" sibling. Both are FFI-shape guarantees, not behaviours.
        Assert.Contains("\"ok\"", ack);
    }
}
