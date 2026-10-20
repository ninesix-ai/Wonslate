// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

using System.ComponentModel;
using System.IO;
using System.Runtime.CompilerServices;
using System.Threading;
using System.Threading.Tasks;
using Wonslate.Audio;

namespace Wonslate.ViewModels;

/// <summary>
/// M4: the voice panel's state machine.
///
/// Split out of MainViewModel because the voice path has its own lifecycle (model
/// presence, a long-running run, playback) and because keeping it separate lets the
/// whole thing be driven by fakes in tests. The pipeline, the model locator, the file
/// picker and the player are all injectable for exactly that reason.
///
/// Model presence is surfaced, never assumed: if the voice models are missing the
/// panel says which ones and refuses to run, instead of failing at the first
/// sherpa call.
/// </summary>
public sealed class VoiceViewModel : INotifyPropertyChanged
{
    private readonly Func<VoiceModelPaths> _locateModels;
    private readonly Func<VoicePipeline>? _pipelineFactory;
    private readonly Func<string?> _pickFile;
    private readonly Action<string> _playWav;
    private readonly Func<float[], int, string, bool>? _saveWav;
    private readonly Func<IAudioCapture> _captureFactory;
    private readonly Func<IStreamingAudioPlayer> _streamingPlayerFactory;

    private VoicePipeline? _pipeline;
    private IAudioCapture? _capture;
    private bool _isRecording;
    private string _filePath = "";
    private string _status = "";
    private string _recognizedText = "";
    private string _translatedText = "";
    private string _timingText = "";
    private string _notes = "";
    private bool _isBusy;
    private float[] _lastSamples = Array.Empty<float>();
    private int _lastRate;

    public VoiceViewModel(
        Func<VoiceModelPaths>? locateModels = null,
        Func<VoicePipeline>? pipelineFactory = null,
        Func<string?>? pickFile = null,
        Action<string>? playWav = null,
        Func<float[], int, string, bool>? saveWav = null,
        Func<IAudioCapture>? captureFactory = null,
        Func<IStreamingAudioPlayer>? streamingPlayerFactory = null)
    {
        _locateModels = locateModels ?? VoiceModelLocator.Resolve;
        _pipelineFactory = pipelineFactory;
        _pickFile = pickFile ?? (() => null);
        _playWav = playWav ?? (_ => { });
        _saveWav = saveWav;
        _captureFactory = captureFactory ?? (() => new MicrophoneCapture());
        _streamingPlayerFactory = streamingPlayerFactory ?? (() => new StreamingAudioPlayer());

        var models = _locateModels();
        ModelsReady = models.IsComplete;
        Status = models.IsComplete
            ? "语音模型已就绪"
            : "缺少语音模型：" + string.Join("、", models.Missing)
              + "。运行 python script/fetch_voice_models.py --only all，或用 "
              + "LT_VOICE_MODEL_DIR / ASR_MODEL_DIR / TTS_MODEL_DIR 指向已有模型目录。";
    }

    /// <summary>True when VAD + ASR + TTS are all on disk.</summary>
    public bool ModelsReady { get; }

    /// <summary>WAV selected for translation.</summary>
    public string FilePath
    {
        get => _filePath;
        private set
        {
            _filePath = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(CanRun));
        }
    }

    public string Status
    {
        get => _status;
        private set { _status = value; OnPropertyChanged(); }
    }

    public string RecognizedText
    {
        get => _recognizedText;
        private set { _recognizedText = value; OnPropertyChanged(); }
    }

    public string TranslatedText
    {
        get => _translatedText;
        private set { _translatedText = value; OnPropertyChanged(); }
    }

    public string TimingText
    {
        get => _timingText;
        private set { _timingText = value; OnPropertyChanged(); }
    }

    /// <summary>Degradation notes, one per line; empty when the run was clean.</summary>
    public string Notes
    {
        get => _notes;
        private set { _notes = value; OnPropertyChanged(); }
    }

    public bool IsBusy
    {
        get => _isBusy;
        private set
        {
            _isBusy = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(IsNotBusy));
            OnPropertyChanged(nameof(CanRun));
        }
    }

    public bool IsNotBusy => !_isBusy;

    public bool CanRun => ModelsReady && !_isBusy && !_isRecording && FilePath.Length > 0;

    public bool CanPlay => !_isBusy && _lastSamples.Length > 0;

    /// <summary>True while the microphone is capturing; the UI shows "停止并翻译" in this state.</summary>
    public bool IsRecording
    {
        get => _isRecording;
        private set
        {
            _isRecording = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(CanRun));
            OnPropertyChanged(nameof(CanRecord));
            OnPropertyChanged(nameof(CanStopRecording));
        }
    }

    public bool CanRecord => ModelsReady && !_isBusy && !_isRecording;

    public bool CanStopRecording => _isRecording;

    /// <summary>Ask the host for a WAV; the dialog itself is injected so tests stay headless.</summary>
    public void SelectFile()
    {
        var picked = _pickFile();
        if (string.IsNullOrWhiteSpace(picked)) return;
        FilePath = picked;
        Status = "已选择：" + Path.GetFileName(picked);
    }

    /// <summary>Use a file directly (tests, drag-drop, CLI).</summary>
    public void SetFile(string path)
    {
        FilePath = path ?? "";
        Status = FilePath.Length > 0 ? "已选择：" + Path.GetFileName(FilePath) : "";
    }

    /// <summary>
    /// Speech in, target-language speech out, off the UI thread. Every failure path
    /// lands in <see cref="Status"/>/<see cref="Notes"/> rather than throwing at the
    /// dispatcher.
    /// </summary>
    public async Task RunAsync(VoiceOptions options, CancellationToken ct = default)
    {
        if (!ModelsReady) { Status = "缺少语音模型，无法运行"; return; }
        if (FilePath.Length == 0) { Status = "请先选择一段 WAV"; return; }

        IsBusy = true;
        Notes = "";
        try
        {
            var (samples, rate) = WavIo.Read(FilePath);
            await RunSamplesAsync(samples, rate, options, ct,
                                  $"输入 {samples.Length / (double)rate:0.0}s / {rate} Hz");
        }
        catch (OperationCanceledException)
        {
            Status = "已取消";
        }
        catch (Exception ex)
        {
            Status = "运行失败：" + ex.Message;
        }
        finally
        {
            IsBusy = false;
        }
    }

    /// <summary>
    /// The one place a buffer becomes a result. File input and microphone input both go through
    /// here so the panel has a single pipeline path (D64: not two implementations that agree until
    /// they do not). Inputs are assumed to already be at <see cref="PcmResampler.TargetRate"/>; the
    /// caller aligns them.
    /// </summary>
    private async Task RunSamplesAsync(float[] samples, int rate, VoiceOptions options,
                                       CancellationToken ct, string sourceLabel)
    {
        Status = $"{sourceLabel}，正在识别与翻译…";

        var pipeline = _pipeline ??= (_pipelineFactory ?? BuildSherpaPipeline)();
        // Streaming playback is created lazily on the first chunk: a non-streaming synthesizer (or a
        // run that produces no audio) never touches an audio device at all.
        IStreamingAudioPlayer? player = null;
        Action<float[], int> onChunk = (chunk, chunkRate) =>
        {
            try
            {
                player ??= _streamingPlayerFactory();
                player.Push(chunk, chunkRate);
            }
            catch (Exception)
            {
                // Best-effort: a missing or busy output device must not fail the transcription run.
            }
        };

        VoiceResult result;
        try
        {
            result = await Task.Run(() => pipeline.Run(samples, rate, options, onChunk), ct);
        }
        finally
        {
            if (player is not null)
            {
                try
                {
                    await Task.Run(() => player.CompleteAndWait(TimeSpan.FromSeconds(30)),
                                   CancellationToken.None);
                }
                finally
                {
                    player.Dispose();
                }
            }
        }

        RecognizedText = result.RecognizedText;
        TranslatedText = result.TranslatedText;
        Notes = string.Join(Environment.NewLine, result.Notes);
        TimingText = $"VAD {result.Timings.SegmentMs:0}ms · ASR {result.Timings.AsrMs:0}ms · "
                   + $"翻译 {result.Timings.TranslateMs:0}ms · TTS {result.Timings.TtsMs:0}ms · "
                   + $"首段可播 {result.Timings.FirstAudioMs:0}ms · 合计 {result.Timings.TotalMs:0}ms";
        _lastSamples = result.Samples;
        _lastRate = result.SampleRate;
        OnPropertyChanged(nameof(CanPlay));

        Status = result.HasAudio
            ? $"完成：{result.Samples.Length / (double)result.SampleRate:0.0}s 目标语言语音"
            : "完成，但没有产出语音（见下方说明）";
    }

    /// <summary>Begin capturing from the microphone; no-op with a reason when that is not possible.</summary>
    public void StartRecording()
    {
        if (!ModelsReady) { Status = "缺少语音模型，无法录音"; return; }
        if (IsBusy) { Status = "正在处理上一次结果，请稍后"; return; }
        if (IsRecording) { Status = "已经在录音了"; return; }
        try
        {
            _capture ??= _captureFactory();
            _capture.Start();
            IsRecording = true;
            Status = "正在录音…说完点“停止并翻译”";
        }
        catch (Exception ex)
        {
            Status = "无法开始录音：" + ex.Message;
        }
    }

    /// <summary>
    /// End the recording, align it to 16 kHz and run the shared pipeline path. A device rate other
    /// than 16 kHz is resampled rather than handed to the VAD (which refuses anything else).
    /// </summary>
    public async Task StopRecordingAndRunAsync(VoiceOptions options, CancellationToken ct = default)
    {
        if (!IsRecording) { Status = "当前没有在录音"; return; }
        CaptureResult captured;
        try
        {
            _capture ??= _captureFactory();
            captured = _capture.Stop();
        }
        catch (Exception ex)
        {
            Status = "停止录音失败：" + ex.Message;
            return;
        }
        finally
        {
            IsRecording = false;
        }

        if (!captured.HasAudio)
        {
            Status = captured.Note ?? "没有录到声音";
            return;
        }

        float[] aligned;
        try
        {
            aligned = PcmResampler.To16k(captured.Samples, captured.SampleRate);
        }
        catch (Exception ex)
        {
            Status = "录音格式无法对齐到 16 kHz：" + ex.Message;
            return;
        }

        IsBusy = true;
        Notes = "";
        try
        {
            var label = $"录音 {captured.DurationSeconds:0.0}s"
                        + (captured.SampleRate == PcmResampler.TargetRate
                            ? ""
                            : $" / {captured.SampleRate} Hz → 16 kHz");
            if (!string.IsNullOrEmpty(captured.Note)) label += $"（{captured.Note}）";
            await RunSamplesAsync(aligned, PcmResampler.TargetRate, options, ct, label);
            if (!string.IsNullOrEmpty(captured.Note))
            {
                // A truncated recording must stay visible next to the result (D80: a short body
                // that reads as complete is the defect this repo keeps paying to remove).
                Notes = Notes.Length > 0 ? Notes + Environment.NewLine + captured.Note : captured.Note!;
            }
        }
        catch (OperationCanceledException)
        {
            Status = "已取消";
        }
        catch (Exception ex)
        {
            Status = "运行失败：" + ex.Message;
        }
        finally
        {
            IsBusy = false;
        }
    }

    /// <summary>Play the last result through the injected player.</summary>
    public void PlayLast()
    {
        if (_lastSamples.Length == 0) { Status = "还没有可播放的语音"; return; }
        try
        {
            var path = Path.Combine(Path.GetTempPath(), "wonslate-voice-last.wav");
            if (_saveWav is null || _saveWav(_lastSamples, _lastRate, path))
            {
                if (_saveWav is null) WavIo.Write(path, _lastSamples, _lastRate);
                _playWav(path);
                Status = "正在播放译文语音";
            }
            else
            {
                Status = "保存临时音频失败，无法播放";
            }
        }
        catch (Exception ex)
        {
            Status = "播放失败：" + ex.Message;
        }
    }

    /// <summary>Production wiring: real sherpa stages. Constructed once, reused per run.</summary>
    private VoicePipeline BuildSherpaPipeline()
    {
        var models = _locateModels();
        var stages = new SherpaVoiceStages(models);
        return new VoicePipeline(stages, stages, stages, new EngineVoiceTranslator());
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void OnPropertyChanged([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}
