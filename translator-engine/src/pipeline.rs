// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 翻译全流水线（L-1 TM → L0 隐私 → L1 覆盖 → L2 本地 → L3 AI）
//!
//! 所有 FFI / REST / CLI 入口最终调用 translate_full()，逻辑统一。

use std::time::Instant;
use crate::{confidence, config, distill, engine, glossary, router, tm};
use crate::error::EngineError;
use crate::types::*;

/// 全功能翻译（含 TM + 路由 + 蒸馏，推荐入口）
pub fn translate_full(mut req: TranslateRequest) -> Result<TranslateResponse, EngineError> {
    let t0 = Instant::now();

    // 若 source_lang == "auto"，MVP 用 heuristic：含 CJK 字符则 zh，否则 en
    if req.source_lang == "auto" || req.source_lang.is_empty() {
        req.source_lang = detect_lang(&req.input);
    }

    // ── L-1: TM 精确命中 ────────────────────────────────────────────
    if req.use_tm && config::get().tm_enabled {
        if let Ok(Some(entry)) = tm::lookup(&req.input, &req.source_lang, &req.target_lang) {
            let _ = tm::increment_hit_count(&entry);
            return Ok(TranslateResponse {
                ok: true,
                engine: "tm".into(),
                source_lang: req.source_lang.clone(),
                target_lang: req.target_lang.clone(),
                input: req.input.clone(),
                output: Some(entry.target_text),
                source: TranslationSource::TmHit,
                latency_ms: t0.elapsed().as_millis() as u64,
                confidence: entry.quality,
                error: None,
                message: None,
            });
        }
    }

    // ── 路由决策 ────────────────────────────────────────────────────
    // 显式 engine_id 视为 Agent 透传锁定：走该引擎且不升级（尊重用户选择）
    let plan = if !req.engine_id.is_empty() {
        router::RoutePlan {
            local_engine_id: req.engine_id.clone(),
            use_glossary: true,
            upgrade_engine_id: None,
            confidence_threshold: 0.0,
        }
    } else {
        router::resolve(&req)
    };

    // ── L2: 本地引擎 + glossary ────────────────────────────────────
    let local_engine = engine::get_engine(&plan.local_engine_id);
    let glossary_ctx = if plan.use_glossary && config::get().glossary_enabled {
        glossary::build_context(
            &req.input, &req.source_lang, &req.target_lang,
            &req.domain, config::get().glossary_max_terms,
        ).unwrap_or_default()
    } else {
        glossary::GlossaryContext::empty()
    };

    let local_output = local_engine.translate_with_context(
        &req.input, &req.source_lang, &req.target_lang, &glossary_ctx,
    );

    if let Some(ref output) = local_output {
        let coverage = glossary_ctx.coverage(&req.input);
        // 置信度按【实际执行的引擎】算，而非路由【声称要用】的引擎。
        // 当声称的 local_engine_id 未注册时，get_engine 会静默回落到 demo，
        // 若仍按声称引擎（如 argos 0.72）算分即为谎报，故用 local_engine.name()。
        let conf = confidence::estimate(output, &req.input, local_engine.name(), coverage);

        // 置信度达标 或 实时档不升级 → 直接返回
        let should_upgrade = conf < plan.confidence_threshold
            && plan.upgrade_engine_id.is_some()
            && !req.privacy
            && req.mode == TranslationMode::Full;

        // 声称的引擎未真正注册（get_engine 已静默回落到 demo）时，记录回落原因，
        // 遵循"不静默降级"：让调用方在 message 里看到"想要 X 却实际用了 Y"。
        let fallback_note: Option<String> = if !engine::is_available(&plan.local_engine_id) {
            Some(format!(
                "requested engine '{}' unavailable, fell back to '{}'",
                plan.local_engine_id, local_engine.name()
            ))
        } else {
            None
        };

        if !should_upgrade {
            let privacy_note = if req.privacy && conf < 0.70 {
                Some("privacy mode: low confidence, AI upgrade prevented".to_string())
            } else {
                None
            };
            let message = match (privacy_note, fallback_note.clone()) {
                (Some(a), Some(b)) => Some(format!("{}; {}", a, b)),
                (Some(a), None)    => Some(a),
                (None, Some(b))    => Some(b),
                (None, None)       => None,
            };
            let resp = TranslateResponse {
                ok: true,
                engine: local_engine.name().into(),
                source_lang: req.source_lang.clone(),
                target_lang: req.target_lang.clone(),
                input: req.input.clone(),
                output: Some(output.clone()),
                source: if req.privacy { TranslationSource::Fallback } else { TranslationSource::Local },
                latency_ms: t0.elapsed().as_millis() as u64,
                confidence: conf,
                error: None,
                message,
            };
            // 写入 TM（quality 达标才入库）
            let min_q = config::get().tm_min_quality;
            if req.use_tm && conf >= min_q {
                let _ = tm::put(&TmEntry {
                    source_text: req.input.clone(),
                    source_lang: req.source_lang.clone(),
                    target_text: output.clone(),
                    target_lang: req.target_lang.clone(),
                    engine: local_engine.name().into(),
                    quality: conf,
                    hit_count: 1,
                    domain: req.domain.clone(),
                });
            }
            return Ok(resp);
        }
    }

    // ── L3: 升级 AI 精译 ───────────────────────────────────────────
    if let Some(ref ai_id) = plan.upgrade_engine_id {
        let ai_engine = engine::get_engine(ai_id);

        // few-shot 注入（层级 2）
        let ai_ctx = if req.use_tm {
            let similar = tm::similar_entries(&req.input, &req.source_lang, &req.target_lang, 5)
                .unwrap_or_default();
            glossary_ctx.clone().with_few_shot(similar)
        } else {
            glossary_ctx.clone()
        };

        let ai_output = ai_engine.translate_with_context(
            &req.input, &req.source_lang, &req.target_lang, &ai_ctx,
        );

        if let Some(output) = ai_output {
            let resp = TranslateResponse {
                ok: true,
                engine: ai_engine.name().into(),
                source_lang: req.source_lang.clone(),
                target_lang: req.target_lang.clone(),
                input: req.input.clone(),
                output: Some(output.clone()),
                source: TranslationSource::AiUpgraded,
                latency_ms: t0.elapsed().as_millis() as u64,
                confidence: 0.95,
                error: None,
                message: None,
            };
            // 写入 TM
            if req.use_tm {
                let _ = tm::put(&TmEntry {
                    source_text: req.input.clone(),
                    source_lang: req.source_lang.clone(),
                    target_text: output.clone(),
                    target_lang: req.target_lang.clone(),
                    engine: ai_engine.name().into(),
                    quality: 0.95,
                    hit_count: 1,
                    domain: req.domain.clone(),
                });
            }
            // 异步蒸馏（层级 1）
            if config::get().distill_enabled {
                distill::submit(DistillTask {
                    source_text: req.input.clone(),
                    source_lang: req.source_lang.clone(),
                    target_text: output.clone(),
                    target_lang: req.target_lang.clone(),
                    domain: req.domain.clone(),
                });
            }
            return Ok(resp);
        }
    }

    // ── 兜底：本地有结果就返回，没有就报 NO_RESULT ─────────────────
    if let Some(output) = local_output {
        Ok(TranslateResponse {
            ok: true,
            engine: local_engine.name().into(),
            source_lang: req.source_lang,
            target_lang: req.target_lang,
            input: req.input,
            output: Some(output),
            source: TranslationSource::Fallback,
            latency_ms: t0.elapsed().as_millis() as u64,
            confidence: 0.60,
            error: None,
            message: Some("AI upgrade failed, returning local result".into()),
        })
    } else {
        // 最终本地兜底：所有引擎都无结果（如 sidecar 缺席）时，显式尝试 demo
        // （全本地、永不联网、隐私安全），带标注不静默；demo 也处理不了才真正 NO_RESULT。
        let demo = engine::get_engine("demo");
        if let Some(out) =
            demo.translate_with_context(&req.input, &req.source_lang, &req.target_lang, &glossary_ctx)
        {
            let conf = confidence::estimate(&out, &req.input, "demo", 0.0);
            return Ok(TranslateResponse {
                ok: true,
                engine: "demo".into(),
                source_lang: req.source_lang,
                target_lang: req.target_lang,
                input: req.input,
                output: Some(out),
                source: TranslationSource::Fallback,
                latency_ms: t0.elapsed().as_millis() as u64,
                confidence: conf,
                error: None,
                message: Some(format!(
                    "engine '{}' produced no result, fell back to local demo",
                    plan.local_engine_id
                )),
            });
        }
        Err(EngineError::NoResult)
    }
}

/// 向后兼容旧 FFI `tt_translate(engine_id, input, lang_pair)` 的实现
pub fn translate_simple(
    engine_id: &str,
    input: &str,
    lang_pair: &str,
) -> Result<TranslateResponse, EngineError> {
    let mut iter = lang_pair.split('-');
    let source = iter.next().unwrap_or("en").trim().to_string();
    let target = iter.next().unwrap_or("zh").trim().to_string();

    let req = TranslateRequest {
        engine_id: engine_id.to_string(),
        input: input.to_string(),
        source_lang: source,
        target_lang: target,
        mode: TranslationMode::Full,
        privacy: false,
        domain: String::new(),
        use_tm: false, // 旧接口不走 TM，保持行为一致
    };
    translate_full(req)
}

/// 简单语言检测（MVP heuristic：含 CJK 字符 → zh，否则 → en）
fn detect_lang(text: &str) -> String {
    if text.chars().any(|c| c >= '\u{4E00}' && c <= '\u{9FFF}') {
        "zh".into()
    } else {
        "en".into()
    }
}

// ── 单元测试（仅覆盖不依赖全局单例的纯函数与简单路径）────────────
//
// translate_full 走完整流水线（依赖 tm/config/engine/distill 单例），
// 归入 tests/pipeline_e2e.rs 集成测试层，见该文件。

#[cfg(test)]
mod tests {
    use super::*;

    // ── detect_lang ────────────────────────────────────────────

    #[test]
    fn detect_lang_pure_chinese_returns_zh() {
        assert_eq!(detect_lang("你好世界"), "zh");
    }

    #[test]
    fn detect_lang_pure_english_returns_en() {
        assert_eq!(detect_lang("Hello, world!"), "en");
    }

    #[test]
    fn detect_lang_mixed_with_any_cjk_returns_zh() {
        // 中英混排：只要含一个 CJK 就判为 zh（与 ASR 层默认策略一致）
        assert_eq!(detect_lang("Hello 世界"), "zh");
    }

    #[test]
    fn detect_lang_empty_returns_en() {
        assert_eq!(detect_lang(""), "en");
    }

    #[test]
    fn detect_lang_punctuation_only_returns_en() {
        assert_eq!(detect_lang("。！，、"), "en");   // 中文标点不属 CJK 统一表意区
    }

    // ── translate_simple（走 demo 引擎，不触碰 TM）─────────────

    #[test]
    fn simple_en_zh_hello_returns_some() {
        // demo 词表覆盖 hello 类基础词
        let r = translate_simple("demo", "hello", "en-zh").unwrap();
        assert!(r.ok);
        assert_eq!(r.engine, "demo");
        assert_eq!(r.source_lang, "en");
        assert_eq!(r.target_lang, "zh");
        let out = r.output.expect("demo hello should translate");
        assert!(out.contains("你好"), "expected 你好, got: {}", out);
    }

    #[test]
    fn simple_unknown_lang_pair_returns_no_result() {
        // demo 只支持 en↔zh，其它语言对返回 None。
        // 用 privacy=true 阻断 AI 升级，让 pipeline 兜底到 Err(NoResult)。
        let req = TranslateRequest {
            engine_id: "demo".into(),
            input: "bonjour".into(),
            source_lang: "fr".into(),
            target_lang: "zh".into(),
            mode: TranslationMode::Full,
            privacy: true,
            domain: String::new(),
            use_tm: false,
        };
        let r = translate_full(req);
        assert!(r.is_err(), "privacy + demo 无法翻 fr-zh，应 NoResult，got: {:?}", r.ok());
    }

    #[test]
    fn simple_lang_pair_parsing() {
        // 验证 lang_pair 分隔符解析：zh-en 应正确拆到 source/target
        let r = translate_simple("demo", "你好", "zh-en").unwrap();
        assert_eq!(r.source_lang, "zh");
        assert_eq!(r.target_lang, "en");
    }

    #[test]
    fn simple_empty_input_is_no_result() {
        // demo 引擎对空字符串返回 None → pipeline 兜底 → Err(NoResult)
        let r = translate_simple("demo", "", "en-zh");
        assert!(r.is_err());
    }

    #[test]
    fn simple_response_has_latency_field() {
        // latency_ms 必须存在，供 UI 展示性能
        let r = translate_simple("demo", "hello", "en-zh").unwrap();
        // 允许为 0（同 CPU 周期太快），但字段必须可解析
        let _ = r.latency_ms;
    }
}
