// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

namespace Wonslate.Routing;

/// <summary>翻译模式：实时档（快）/ 精译档（质量优先）。</summary>
public enum TranslationMode
{
    Realtime,
    Full,
}

/// <summary>单条静态路由规则（对应设计方案 §3.2 规则表）。</summary>
public sealed record RouteRule(
    TranslationMode Mode,
    bool PrivacySensitive,
    string? LangPairPattern, // 形如 "en-zh" / "zh-en" / "*" 表示任意
    string EngineId)
{
    public bool Matches(TranslationMode mode, bool privacy, string langPair)
    {
        if (mode != Mode) return false;
        if (privacy != PrivacySensitive) return false;
        if (LangPairPattern is null or "*") return true;
        return LangPairPattern.Equals(langPair, StringComparison.OrdinalIgnoreCase);
    }

    public override string ToString() =>
        $"{(PrivacySensitive ? "[隐私] " : "")}[{Mode}] {LangPairPattern ?? "*"} -> {EngineId}";
}

/// <summary>
/// 静态路由引擎：按「隐私 -> 覆盖 -> 质量/成本」优先级匹配规则表，选择翻译引擎。
/// MVP 先用硬编码规则，后续替换为配置文件驱动（YAML/JSON）。
/// </summary>
public sealed class Router
{
    private readonly IReadOnlyList<RouteRule> _rules;

    public Router()
    {
        _rules = BuildDefaultRules();
    }

    public IReadOnlyList<RouteRule> Rules => _rules;

    /// <summary>对一次翻译请求做路由决策，返回应使用的引擎 id。</summary>
    public string Resolve(TranslationMode mode, bool privacySensitive, string langPair)
    {
        // L0 隐私层：敏感内容强制本地 demo（后续替换为合规离线引擎）
        if (privacySensitive)
        {
            return "demo";
        }

        // L1/L2 规则层：按序匹配
        foreach (var rule in _rules)
        {
            if (rule.Matches(mode, privacySensitive, langPair))
            {
                return rule.EngineId;
            }
        }

        // fallback：永远回到 demo，保证不空路由
        return "demo";
    }

    private static List<RouteRule> BuildDefaultRules()
    {
        return
        [
            // 实时档：快速低延迟，仍用 demo 词表引擎（后续可换本地小模型）
            new RouteRule(TranslationMode.Realtime, false, "*", "demo"),
            // 精译档：质量优先，接入本机 Ollama 大模型（qwen3:8b）
            new RouteRule(TranslationMode.Full, false, "*", "ollama"),
        ];
    }
}
