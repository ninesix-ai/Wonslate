// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 ninesix-ai studio

//! 许可合规门禁（零外部依赖）
//!
//! 把组件/模型许可清单编成数据，用黑/白名单校验，
//! 确保任何进入闭源商用产物的依赖都是宽松可商用许可（Apache/MIT/BSD/ISC/CC0…）。
//!
//! 设计要点：本项目 crate 侧几乎零依赖（仅 serde_json 树），真正的许可红线在
//! 【模型/第三方组件】（NLLB/SeamlessM4T=CC-BY-NC、LibreTranslate=AGPL），
//! 这些是 cargo-deny 看不到的，故用本模块显式登记并门禁。

// 实现占位：以下函数与常量在 GREEN 阶段补齐。

/// 依赖类型（crate / 模型权重 / 第三方组件）
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Kind {
    Crate,
    Model,
    Component,
}

/// 一条已登记的依赖及其许可证标识。
#[derive(Debug, Clone, Copy)]
pub struct Dep {
    pub name: &'static str,
    pub license: &'static str,
    pub kind: Kind,
}

/// 宽松可闭源商用白名单（精确匹配，默认拒绝）。
pub const ALLOWED_LICENSES: &[&str] = &[
    "Apache-2.0",
    "MIT",
    "ISC",
    "BSD-3-Clause",
    "CC0-1.0",
];

/// 已知危险黑名单（仅用于自检与告警；判定以白名单为准）。
pub const DENIED_LICENSES: &[&str] = &[
    "CC-BY-NC-4.0",  // NLLB-200 / SeamlessM4T / Aya：仅非商用
    "AGPL-3.0",      // LibreTranslate 服务端：网络传染
    "GPL-3.0",       // 强 copyleft
];

/// 当前产物实际使用的组件/模型/crate 许可清单。
pub const REGISTERED_DEPS: &[Dep] = &[
    Dep { name: "serde_json",   license: "MIT",        kind: Kind::Crate },
    Dep { name: "sherpa-onnx",  license: "Apache-2.0", kind: Kind::Component },
    Dep { name: "sensevoice",   license: "MIT",        kind: Kind::Model },
    Dep { name: "kokoro",       license: "Apache-2.0", kind: Kind::Model },
    Dep { name: "qwen3",        license: "Apache-2.0", kind: Kind::Model },
    Dep { name: "ollama",       license: "MIT",        kind: Kind::Component },
    Dep { name: "naudio",       license: "MIT",        kind: Kind::Component },
];

/// 许可证是否宽松可商用（白名单精确匹配，未知一律拒绝）。
pub fn is_allowed(license: &str) -> bool {
    ALLOWED_LICENSES.contains(&license)
}

/// 校验一组依赖，返回所有许可违规项的 name（空=全合规）。
pub fn audit(deps: &[Dep]) -> Vec<&'static str> {
    deps.iter()
        .filter(|d| !is_allowed(d.license))
        .map(|d| d.name)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn permissive_licenses_are_allowed() {
        assert!(is_allowed("Apache-2.0"));
        assert!(is_allowed("MIT"));
        assert!(is_allowed("ISC"));
        assert!(is_allowed("BSD-3-Clause"));
        assert!(is_allowed("CC0-1.0"));
    }

    #[test]
    fn copyleft_and_noncommercial_licenses_are_denied() {
        assert!(!is_allowed("CC-BY-NC-4.0"));
        assert!(!is_allowed("AGPL-3.0"));
        assert!(!is_allowed("GPL-3.0"));
        // 未知许可一律拒绝（默认拒绝，而非默认放行）
        assert!(!is_allowed("Weird-Unknown-License"));
    }

    #[test]
    fn allowed_and_denied_lists_are_disjoint() {
        for d in DENIED_LICENSES {
            assert!(!ALLOWED_LICENSES.contains(d),
                "许可 {} 同时出现在黑/白名单，存在矛盾", d);
        }
    }

    #[test]
    fn registered_deps_are_all_compliant() {
        // 真实登记清单（06 §1.4.4）必须全部合规，否则门禁形同虚设
        let violations = audit(&REGISTERED_DEPS);
        assert!(violations.is_empty(), "登记的组件/模型存在许可违规: {:?}", violations);
    }

    #[test]
    fn audit_flags_a_poisoned_entry() {
        let poisoned = [
            Dep { name: "nllb-200", license: "CC-BY-NC-4.0", kind: Kind::Model },
            Dep { name: "libretranslate", license: "AGPL-3.0", kind: Kind::Component },
        ];
        let v = audit(&poisoned);
        assert!(v.contains(&"nllb-200"), "CC-BY-NC 模型应被拦截");
        assert!(v.contains(&"libretranslate"), "AGPL 组件应被拦截");
    }

    #[test]
    fn registered_deps_cover_key_models() {
        // 防止清单被误清空：核心可商用模型必须登记在册
        let names: Vec<_> = REGISTERED_DEPS.iter().map(|d| d.name).collect();
        for expected in ["qwen3", "sensevoice", "kokoro", "sherpa-onnx"] {
            assert!(names.contains(&expected), "核心组件 {} 未在许可清单登记", expected);
        }
    }
}
