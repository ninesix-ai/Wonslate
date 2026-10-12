// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Threading;
using System.Threading.Tasks;
using Wonslate.Interop;

namespace Wonslate.ViewModels;

/// <summary>Main-window ViewModel: binds input, mode, the privacy toggle and the translation chain.</summary>
public sealed class MainViewModel : INotifyPropertyChanged
{
    private string _input = "Hello there";
    private string _output = "";
    private bool _isBusy;
    private bool _privacySensitive;
    private string _mode = "realtime";
    private string _engineLabel = "";
    private string _sourceLang = "en";
    private string _targetLang = "zh";
    private string _detectedSourceLang = "";
    private string _statusMessage = "";
    private string _diagnostics = "";
    private string _glossarySourceTerm = "";
    private string _glossaryTargetTerm = "";
    private string _glossaryDomainFilter = "";
    private TmEntryDto? _selectedTmEntry;
    private GlossaryEntryDto? _selectedGlossaryEntry;

    /// <summary>
    /// The shell supplies the voice panel so it can carry the real audio player and
    /// model locator; tests get a silent default that never touches a device.
    /// </summary>
    public MainViewModel(VoiceViewModel? voice = null) => Voice = voice ?? new VoiceViewModel();

    public string Input
    {
        get => _input;
        set { _input = value; OnPropertyChanged(); }
    }

    public string Output
    {
        get => _output;
        private set { _output = value; OnPropertyChanged(); }
    }

    public bool IsBusy
    {
        get => _isBusy;
        private set
        {
            _isBusy = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(IsNotBusy));
        }
    }

    /// <summary>Inverse of <see cref="IsBusy"/>, so buttons can be disabled while a call runs.</summary>
    public bool IsNotBusy => !_isBusy;

    /// <summary>
    /// Startup diagnostics: the engine-init ack plus one entry per sidecar endpoint.
    /// Pushed by the application shell so a failed init or sidecar is visible instead of silent.
    /// </summary>
    public string Diagnostics
    {
        get => _diagnostics;
        private set { _diagnostics = value; OnPropertyChanged(); }
    }

    /// <summary>Replace the startup diagnostics line (called from the app shell).</summary>
    public void SetDiagnostics(string text) => Diagnostics = text ?? "";

    public bool PrivacySensitive
    {
        get => _privacySensitive;
        set { _privacySensitive = value; OnPropertyChanged(); }
    }

    public string Mode
    {
        get => _mode;
        set { _mode = value; OnPropertyChanged(); }
    }

    /// <summary>Current routing/engine label (includes TM hit state).</summary>
    public string EngineLabel
    {
        get => _engineLabel;
        private set { _engineLabel = value; OnPropertyChanged(); }
    }

    public string EngineVersion => EngineNative.Version();

    /// <summary>Source-language code that asks the engine to detect it (pipeline.rs: "auto" or empty).</summary>
    public const string AutoDetectCode = "auto";

    /// <summary>One selectable language: the engine code plus the label shown in the UI.</summary>
    public sealed record LangOption(string Code, string DisplayName);

    /// <summary>
    /// Languages the router treats as common (router.rs is_common_pair). Selecting one
    /// still needs the matching local model to translate offline; without it the
    /// pipeline reports an explicit fallback instead of pretending to succeed.
    /// </summary>
    private static readonly LangOption[] RealLanguages =
    {
        new LangOption("zh", "中文"),
        new LangOption("en", "English"),
        new LangOption("ja", "日本語"),
        new LangOption("ko", "한국어"),
        new LangOption("fr", "Français"),
        new LangOption("de", "Deutsch"),
        new LangOption("es", "Español"),
        new LangOption("ru", "Русский"),
        new LangOption("pt", "Português"),
        new LangOption("it", "Italiano"),
        new LangOption("ar", "العربية"),
    };

    /// <summary>Concrete languages, offered for both sides of the pair.</summary>
    public IReadOnlyList<LangOption> Languages => RealLanguages;

    /// <summary>Source choices: auto-detection first, then every concrete language.</summary>
    public IReadOnlyList<LangOption> SourceLanguages { get; } =
        new[] { new LangOption(AutoDetectCode, "自动检测") }.Concat(RealLanguages).ToArray();

    /// <summary>Source language code (a concrete code, or "auto" to let the engine detect it).</summary>
    public string SourceLang
    {
        get => _sourceLang;
        set
        {
            _sourceLang = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(CanExchangeLanguages));
            OnPropertyChanged(nameof(EffectiveSourceLang));
            OnPropertyChanged(nameof(CanManageGlossary));
        }
    }

    /// <summary>Target language code (always concrete: nothing can be translated into "auto").</summary>
    public string TargetLang
    {
        get => _targetLang;
        set { _targetLang = value; OnPropertyChanged(); }
    }

    /// <summary>Swapping needs a concrete source; "auto" has no meaningful target side.</summary>
    public bool CanExchangeLanguages => _sourceLang != AutoDetectCode;

    /// <summary>Swap source and target languages (no-op while the source is auto-detected).</summary>
    public void ExchangeLanguages()
    {
        if (!CanExchangeLanguages) return;
        (_sourceLang, _targetLang) = (_targetLang, _sourceLang);
        OnPropertyChanged(nameof(SourceLang));
        OnPropertyChanged(nameof(TargetLang));
    }

    /// <summary>Build the v2.0 pipeline request JSON (follows SourceLang/TargetLang; testable).</summary>
    public string BuildRequestJson(string text) =>
        System.Text.Json.JsonSerializer.Serialize(new
        {
            input = text,
            source_lang = SourceLang,
            target_lang = TargetLang,
            mode = Mode == "realtime" ? "realtime" : "full",
            privacy = PrivacySensitive,
            use_tm = true,
        });

    // ---- TM / glossary management (N-07) -----------------------------------------

    /// <summary>Source language reported by the engine for the last successful translation.</summary>
    public string DetectedSourceLang
    {
        get => _detectedSourceLang;
        private set
        {
            _detectedSourceLang = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(EffectiveSourceLang));
            OnPropertyChanged(nameof(CanManageGlossary));
        }
    }

    /// <summary>
    /// Concrete source language used for TM / glossary writes. "auto" is resolved from the
    /// last detection; it stays empty when nothing has been translated yet, so a write can
    /// report the real reason instead of guessing a language.
    /// </summary>
    public string EffectiveSourceLang =>
        SourceLang != AutoDetectCode ? SourceLang : _detectedSourceLang;

    /// <summary>Glossary keys need a concrete source language; auto-detection must resolve first.</summary>
    public bool CanManageGlossary => EffectiveSourceLang.Length > 0;

    /// <summary>Feedback line for TM / glossary operations (empty when there is nothing to say).</summary>
    public string StatusMessage
    {
        get => _statusMessage;
        private set { _statusMessage = value; OnPropertyChanged(); }
    }

    /// <summary>Voice panel (M4): owns its own model/pipeline lifecycle.</summary>
    public VoiceViewModel Voice { get; }

    /// <summary>Settings page (D15): quality/cost preference, applied on restart.</summary>
    public SettingsViewModel Settings { get; } = new();

    /// <summary>Active TM entries of the selected pair, most-hit first.</summary>
    public ObservableCollection<TmEntryDto> TmEntries { get; } = new();

    /// <summary>Glossary terms of the selected pair, highest confidence first.</summary>
    public ObservableCollection<GlossaryEntryDto> GlossaryEntries { get; } = new();

    public TmEntryDto? SelectedTmEntry
    {
        get => _selectedTmEntry;
        set { _selectedTmEntry = value; OnPropertyChanged(); }
    }

    public GlossaryEntryDto? SelectedGlossaryEntry
    {
        get => _selectedGlossaryEntry;
        set { _selectedGlossaryEntry = value; OnPropertyChanged(); }
    }

    public string GlossarySourceTerm
    {
        get => _glossarySourceTerm;
        set { _glossarySourceTerm = value; OnPropertyChanged(); }
    }

    public string GlossaryTargetTerm
    {
        get => _glossaryTargetTerm;
        set { _glossaryTargetTerm = value; OnPropertyChanged(); }
    }

    /// <summary>
    /// S11: the domain scope of the glossary view. Empty shows only generic
    /// rows (the pre-S11 default); a named value such as "av" shows generic ∪
    /// that domain, with the specific row replacing an identically-named
    /// generic one. Setting this property re-queries the store so the visible
    /// list tracks the filter without a manual refresh.
    /// </summary>
    public string GlossaryDomainFilter
    {
        get => _glossaryDomainFilter;
        set
        {
            if (_glossaryDomainFilter == value) return;
            _glossaryDomainFilter = value ?? "";
            OnPropertyChanged();
            RefreshGlossary();
        }
    }

    /// <summary>Serialized TM entry for a user-confirmed pair (pure; unit-tested).</summary>
    public static string BuildTmEntryJson(
        string sourceText, string sourceLang, string targetText, string targetLang) =>
        System.Text.Json.JsonSerializer.Serialize(new
        {
            source_text = sourceText,
            source_lang = sourceLang,
            target_text = targetText,
            target_lang = targetLang,
            engine      = "manual",
            quality     = 1.0,
            hit_count   = 1,
            domain      = "",
        }, EngineNative.SnakeCase);

    /// <summary>Serialized glossary term for a user edit (pure; unit-tested).
    /// <paramref name="locked"/> travels as an explicit opinion: "saving a term means I
    /// own it" only holds if the payload says so, and the engine reads an absent key as
    /// "leave the lock as it is".</summary>
    public static string BuildGlossaryEntryJson(
        string sourceTerm, string sourceLang, string targetTerm, string targetLang,
        string domain = "", bool locked = false) =>
        System.Text.Json.JsonSerializer.Serialize(new
        {
            source_term = sourceTerm,
            source_lang = sourceLang,
            target_term = targetTerm,
            target_lang = targetLang,
            confidence  = 1.0,
            frequency   = 1,
            domain,
            user_locked = locked,
        }, EngineNative.SnakeCase);

    /// <summary>Payload that re-saves a listed row with a new lock opinion (pure; unit-tested).
    /// Every other field is carried over from the row itself -- including its domain, so
    /// locking a seed-pack row addresses that row instead of creating a generic twin (D35).
    /// The engine stamps provenance as `manual`; this is a user action by definition.</summary>
    public static string BuildGlossaryLockJson(GlossaryEntryDto row, bool locked) =>
        System.Text.Json.JsonSerializer.Serialize(new
        {
            source_term = row.SourceTerm,
            source_lang = row.SourceLang,
            target_term = row.TargetTerm,
            target_lang = row.TargetLang,
            confidence  = row.Confidence,
            frequency   = row.Frequency,
            domain      = row.Domain,
            user_locked = locked,
        }, EngineNative.SnakeCase);

    /// <summary>What the status line says after a glossary save, read from the engine's own
    /// echo rather than assumed: the row is locked only if the store says it is.</summary>
    public static string GlossarySaveMessage(AckDto ack) => ack.Ok switch
    {
        false => $"术语保存失败：{ack.Message}",
        true when ack.UserLocked == true => "术语已保存并锁定",
        _ => "术语已保存",
    };

    /// <summary>What the status line says after a lock/unlock click, from the engine's echo.
    /// "已锁定" is the promise that matters to the user, so it must not be printed when the
    /// engine did not confirm it.</summary>
    public static string GlossaryLockMessage(AckDto ack, bool locked) => ack.Ok switch
    {
        false => $"锁定操作失败：{ack.Message}",
        true when ack.UserLocked == null => locked
            ? "已请求锁定，但引擎未回报当前状态"
            : "已请求解锁，但引擎未回报当前状态",
        true when ack.UserLocked == true => "该术语已锁定：蒸馏与术语包不会再改它",
        _ => "该术语已解锁：蒸馏与术语包可以再次更新它",
    };

    /// <summary>What the status line says after a delete. The FFI key has no domain segment,
    /// so a domain-scoped row is not removable through it -- and `removed:false` must not be
    /// rendered as a deletion (measured: it used to say "术语已删除" over a row still on
    /// screen).</summary>
    public static string GlossaryDeleteMessage(AckDto ack, GlossaryEntryDto row) => ack.Ok switch
    {
        false => $"术语删除失败：{ack.Message}",
        true when ack.Removed == false =>
            $"未删除：「{row.SourceTerm}」属于领域 {row.Domain}，当前删除只作用于通用行",
        _ => "术语已删除",
    };

    /// <summary>Refusal for a delete the engine cannot aim (D50). The FFI delete key carries
    /// no domain segment, so a domain-scoped row is out of its reach -- and when the same
    /// term also exists as a generic row, that other row is what would be removed. Saying no
    /// is the honest half; the signature that aims at a domain row is a contract change.
    /// Returns null when the delete may go ahead.</summary>
    public static string? GlossaryDeleteGuard(GlossaryEntryDto row) =>
        row.Domain.Length == 0 ? null
            : $"未删除：「{row.SourceTerm}」属于领域 {row.Domain}，当前删除只作用于通用行";

    /// <summary>Reload the TM list for the selected language pair.</summary>
    public void RefreshTm()
    {
        var src = EffectiveSourceLang;
        if (src.Length == 0) { StatusMessage = "源语言为自动检测，请先翻译一次再查看 TM"; return; }
        TmEntries.Clear();
        foreach (var e in TmEntryDto.ListFromJson(EngineNative.TmList(src, TargetLang)))
            TmEntries.Add(e);
        StatusMessage = $"TM 共 {TmEntries.Count} 条（{src}→{TargetLang}）";
    }

    /// <summary>Reload the glossary for the selected language pair.</summary>
    public void RefreshGlossary()
    {
        var src = EffectiveSourceLang;
        if (src.Length == 0) { StatusMessage = "源语言为自动检测，请先翻译一次再查看术语表"; return; }
        GlossaryEntries.Clear();
        // S11: pass the current domain filter through to the store. An empty
        // filter is the "generic only" query, matching the pre-S11 UI surface.
        var json = EngineNative.GlossaryList(src, TargetLang, GlossaryDomainFilter ?? "", 500);
        foreach (var e in GlossaryEntryDto.ListFromJson(json))
            GlossaryEntries.Add(e);
        var scope = string.IsNullOrEmpty(GlossaryDomainFilter) ? "" : " / " + GlossaryDomainFilter;
        StatusMessage = $"术语表共 {GlossaryEntries.Count} 条（{src}→{TargetLang}{scope}）";
    }

    /// <summary>Store the current input/output pair into the TM (user-confirmed).</summary>
    public void SaveCurrentToTm()
    {
        var srcText = Input?.Trim() ?? "";
        var tgtText = Output?.Trim() ?? "";
        if (srcText.Length == 0 || tgtText.Length == 0)
        {
            StatusMessage = "没有可存入的译文（先翻译一次）";
            return;
        }
        var srcLang = EffectiveSourceLang;
        if (srcLang.Length == 0)
        {
            StatusMessage = "源语言为自动检测，请先翻译一次再存入 TM";
            return;
        }
        var ack = AckDto.FromJson(
            EngineNative.TmPut(BuildTmEntryJson(srcText, srcLang, tgtText, TargetLang)));
        StatusMessage = ack.Ok ? "已存入 TM" : $"存入 TM 失败：{ack.Message}";
        if (ack.Ok) RefreshTm();
    }

    /// <summary>Mark the selected TM entry bad: it stops being served and leaves the list.</summary>
    public void FlagBadSelectedTm()
    {
        if (SelectedTmEntry is null) { StatusMessage = "请先选择一条 TM 记录"; return; }
        var e = SelectedTmEntry;
        var ack = AckDto.FromJson(EngineNative.TmFlagBad(e.SourceText, e.SourceLang, e.TargetLang));
        StatusMessage = ack.Ok ? "已标坏，该条不再命中" : $"标坏失败：{ack.Message}";
        if (ack.Ok) { SelectedTmEntry = null; RefreshTm(); }
    }

    /// <summary>Insert or update one glossary term from the two input boxes.</summary>
    public void SaveGlossaryTerm()
    {
        var term = GlossarySourceTerm?.Trim() ?? "";
        var translation = GlossaryTargetTerm?.Trim() ?? "";
        if (term.Length == 0 || translation.Length == 0)
        {
            StatusMessage = "术语原文与译文都不能为空";
            return;
        }
        var srcLang = EffectiveSourceLang;
        if (srcLang.Length == 0)
        {
            StatusMessage = "源语言为自动检测，请先翻译一次再编辑术语表";
            return;
        }
        var ack = AckDto.FromJson(EngineNative.GlossaryUpsert(
            BuildGlossaryEntryJson(term, srcLang, translation, TargetLang, "", true)));
        StatusMessage = GlossarySaveMessage(ack);
        if (ack.Ok) { GlossarySourceTerm = ""; GlossaryTargetTerm = ""; RefreshGlossary(); }
    }

    /// <summary>Lock or unlock the selected glossary term (D35).</summary>
    public void SetSelectedGlossaryLock(bool locked)
    {
        if (SelectedGlossaryEntry is null) { StatusMessage = "请先选择一条术语"; return; }
        var e = SelectedGlossaryEntry;
        var ack = AckDto.FromJson(EngineNative.GlossaryUpsert(BuildGlossaryLockJson(e, locked)));
        StatusMessage = GlossaryLockMessage(ack, locked);
        if (ack.Ok) RefreshGlossary();
    }

    /// <summary>Delete the selected glossary term.</summary>
    public void DeleteSelectedGlossaryTerm()
    {
        if (SelectedGlossaryEntry is null) { StatusMessage = "请先选择一条术语"; return; }
        var e = SelectedGlossaryEntry;
        var guard = GlossaryDeleteGuard(e);
        if (guard is not null) { StatusMessage = guard; return; }
        var ack = AckDto.FromJson(
            EngineNative.GlossaryDelete(e.SourceTerm, e.SourceLang, e.TargetLang));
        StatusMessage = GlossaryDeleteMessage(ack, e);
        if (ack.Ok && ack.Removed != false) { SelectedGlossaryEntry = null; RefreshGlossary(); }
    }

    // ---- Translation (N-06: async path + cancel + timeout + one retry) -------------

    /// <summary>
    /// Raw engine call seam. Production uses the FFI; tests inject a fake so the
    /// success / timeout / cancel paths can be exercised without the native library.
    /// </summary>
    internal Func<string, string> InvokeTranslate { get; set; } = EngineNative.TranslateFull;

    /// <summary>
    /// Told which engine actually served each successful request.
    ///
    /// The shell uses this to warm exactly that sidecar endpoint. It is a report and
    /// not a guess: deriving the engine from the mode would copy Rust's routing rules into
    /// the UI, where they can drift out of step with the router in silence.
    /// </summary>
    internal Action<string>? OnEngineUsed { get; set; }

    /// <summary>Per-attempt timeout for the async path. The native call cannot be aborted,
    /// so an overrun is abandoned (logical cancellation) rather than applied late. The default
    /// is a backstop, not a first line: it sits ABOVE the Rust client's own worst case
    /// (`busy_retry 12 s + *_SIDECAR_TIMEOUT_MS 30 s = 42 s`) so the UI does not abandon a call
    /// the engine is legitimately still waiting out (a measured page-scale madlad decode is
    /// ~25 s). Env-overridable via <see cref="ResolveTranslateTimeout"/>; the abandoned native
    /// call still keeps decoding, so this value does not cancel server work -- see the retry note.
    /// </summary>
    public TimeSpan TranslateTimeout { get; set; } = TimeSpan.FromMilliseconds(DefaultTranslateTimeoutMs);

    /// <summary>Extra attempts after the first one. Default 0: a retry re-issues a whole native
    /// call while the abandoned one is STILL decoding, and with the decode gate off by default the
    /// two overlap and slow the work that is wanted (defect D30 item 2, measured 2.5x). The retry
    /// is kept as an opt-in knob (tests pin it to 1) rather than a shipped amplifier.
    /// </summary>
    public int TranslateRetries { get; set; } = 0;

    /// <summary>Shipped default for the outermost (UI) per-attempt timeout.</summary>
    public const int DefaultTranslateTimeoutMs = 45_000;

    /// <summary>
    /// Resolve the UI per-attempt translate timeout from the environment, honoring the shared
    /// LT_* (legacy) &gt; WONSLATE_* (brand) prefix chain. Absent, blank, unparsable or
    /// non-positive all fall back to <see cref="DefaultTranslateTimeoutMs"/>: a typo must not
    /// silently turn into an unbounded wait. The env reader is injected so resolution is hermetic
    /// -- a unit test never depends on the host environment (the D36/D45 class of mistake). This
    /// knob is read on the .NET side only; it is not a Rust engine config and so is deliberately
    /// outside the Rust hermetic KNOBS table.
    /// </summary>
    public static TimeSpan ResolveTranslateTimeout(Func<string, string?> env)
    {
        static string? NonBlank(Func<string, string?> read, string key)
        {
            var value = read(key);
            return string.IsNullOrWhiteSpace(value) ? null : value;
        }
        var raw = NonBlank(env, "LT_UI_TRANSLATE_TIMEOUT_MS")
                  ?? NonBlank(env, "WONSLATE_UI_TRANSLATE_TIMEOUT_MS");
        return int.TryParse(raw, out var ms) && ms > 0
            ? TimeSpan.FromMilliseconds(ms)
            : TimeSpan.FromMilliseconds(DefaultTranslateTimeoutMs);
    }

    private CancellationTokenSource? _inFlight;

    /// <summary>Ask the running translation to give up (the cancel button).</summary>
    public void CancelTranslate() => _inFlight?.Cancel();

    /// <summary>
    /// Run one translation through the v2.0 full pipeline (TM + routing + distillation).
    /// Synchronous entry point kept for callers and tests that run without a UI thread.
    /// </summary>
    public void Translate()
    {
        var text = Input?.Trim() ?? "";
        if (text.Length == 0) { Output = ""; return; }

        IsBusy = true;
        try
        {
            ApplyResponse(InvokeTranslate(BuildRequestJson(text)));
        }
        catch (Exception ex)
        {
            EngineLabel = "调用失败";
            Output = $"异常: {ex.Message}";
        }
        finally
        {
            IsBusy = false;
        }
    }

    /// <summary>
    /// Real UI path: runs the engine off the dispatcher thread so a slow engine
    /// (for example an ollama timeout in full mode) never freezes the window.
    /// </summary>
    public async Task TranslateAsync(CancellationToken cancellationToken = default)
    {
        var text = Input?.Trim() ?? "";
        if (text.Length == 0) { Output = ""; return; }

        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        _inFlight = cts;
        IsBusy = true;
        try
        {
            ApplyResponse(await RunWithRetryAsync(BuildRequestJson(text), cts.Token));
        }
        catch (OperationCanceledException)
        {
            EngineLabel = "已取消";
            StatusMessage = "翻译已取消";
        }
        catch (TimeoutException)
        {
            EngineLabel = "超时";
            Output = $"翻译超时（{TranslateTimeout.TotalSeconds:0.#}s）。可重试，或改用实时档 / 无需联网的本地模型。";
        }
        catch (Exception ex)
        {
            EngineLabel = "调用失败";
            Output = $"异常: {ex.Message}";
        }
        finally
        {
            _inFlight = null;
            IsBusy = false;
        }
    }

    /// <summary>One attempt plus the configured number of retries on timeout.</summary>
    private async Task<string> RunWithRetryAsync(
        string requestJson, CancellationToken ct)
    {
        for (int attempt = 0; ; attempt++)
        {
            try
            {
                return await RunOnceAsync(requestJson, ct);
            }
            catch (TimeoutException) when (attempt < TranslateRetries)
            {
                StatusMessage = $"第 {attempt + 1} 次尝试超时，正在重试";
            }
        }
    }

    /// <summary>
    /// One call raced against the timeout. The native call keeps running in the
    /// background when it loses the race; its result is dropped, never applied late.
    /// </summary>
    private async Task<string> RunOnceAsync(
        string requestJson, CancellationToken ct)
    {
        var invoke = InvokeTranslate;
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TranslateTimeout);

        var work = Task.Run(() => invoke(requestJson), ct);
        var abandoned = Task.Delay(Timeout.Infinite, timeout.Token);

        if (await Task.WhenAny(work, abandoned) != work)
        {
            Observe(work);
            ct.ThrowIfCancellationRequested();
            throw new TimeoutException($"engine call exceeded {TranslateTimeout.TotalSeconds:0.#}s");
        }
        return await work;
    }

    /// <summary>Attach a handler so a late fault on an abandoned call is never unobserved.</summary>
    private static void Observe(Task task) =>
        task.ContinueWith(
            t => _ = t.Exception,
            CancellationToken.None,
            TaskContinuationOptions.OnlyOnFaulted
                | TaskContinuationOptions.ExecuteSynchronously,
            TaskScheduler.Default);

    /// <summary>Map one raw engine response onto the UI state (shared by both entry points).</summary>
    internal void ApplyResponse(string json)
    {
        var result = EngineNative.TranslateResponseDto.FromJson(json);

        // surface where the result came from
        var srcLabel = result.Source switch
        {
            "tm_hit"      => "TM 命中",
            "local"       => "本地引擎",
            "ai_upgraded" => "AI 精译",
            "fallback"    => "兜底降级",
            _             => result.Source,
        };
        EngineLabel = $"[{srcLabel}] {result.Engine} · {result.LatencyMs}ms";

        // The endpoint that just served is the one worth keeping resident: tell the shell
        // before anything else can overwrite this result.
        if (result.Ok && result.Engine.Length > 0) OnEngineUsed?.Invoke(result.Engine);

        // The engine resolves "auto" to a concrete code; TM / glossary writes need it.
        if (result.SourceLang.Length > 0) DetectedSourceLang = result.SourceLang;

        Output = result.Ok
            ? result.Output ?? "(无输出)"
            : $"[{result.Error}] {result.Message}";
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void OnPropertyChanged([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}
