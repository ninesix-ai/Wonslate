# AGENTS.md — how to work in this repository

This file is for AI coding agents and for contributors who are about to edit files.
It carries only what is written down nowhere else: the entry points that actually
work, the traps that have already cost a session, and where project truth is kept.

- Product overview, prerequisites, demo inputs: `README.md`
- Conventions, invariants, pull requests: `CONTRIBUTING.md`

## 1. What this repo is

Wonslate（万邦译）is a local, offline, multi-engine translation engine. The call chain
is WPF UI (C#) → C ABI FFI → Rust `translator_engine` cdylib → Python CT2 sidecar
(Argos / MADLAD), with a local Ollama engine behind the quality tier. Four languages
live in one build: Rust, C#, Python, XAML.

Requirements, task status and the commercial plan are **not in this repository**. They
live in a private umbrella repo that pins this one as a git submodule. Nothing below
quotes their numbers, because those numbers move.

## 2. Work here — only these entry points

Everything runs from the repo root through `script/build.py`. `build.bat` and
`build.sh` are thin per-OS forwarders that pass every argument through.

| Intent | Command |
|---|---|
| Build only | `python script/build.py` |
| Build + every test layer | `python script/build.py --test` |
| Build + Rust and .NET tests | `python script/build.py --unit` |
| Build + the Python suites | `python script/build.py --ffi` |
| Build and start the GUI | `python script/build.py --run` |
| XAML static lint + window-appears smoke | `verify.bat` / `verify.sh` |
| Self-sign output so local smokes repeat | `sign.bat` |
| Measure what a long batch actually costs (D24/D30 probes; `--help` for the eight modes; `reload` prices a self-reset, `abandon` prices work nobody waits for, `gate` picks the decode-slot limit, `queue` sizes the queue a batch of waiters needs, and `--corpus` drives a run with distinct sentences reported pass against pass) | `python script/diag_sidecar_batch.py load --corpus script/eval-data/av-zh-en --n 900 --out-json run.json` |
| Check an output for invisible characters, on either or both engine tiers (D42) | `python script/diag_invisible_chars.py both` |
| Quote test counts from a gate log. It refuses to report while any layer is unattributed, and `run` pins the CLI's output language so the log stays readable (a localised or UTF-16 log makes every pattern miss, which reads like a layer that never ran) | `python script/gate_counts.py parse <log>` / `... run [--log NAME]` |
| Keep every name-based decision honest about the value behind the name (D68 privacy whitelist vs `*_URL`; D69 `REGISTERED_DEPS` vs the bytes a downloader actually fetched). `--self-test` plants one violation per rule class and requires all of them to fire, so a rule that stopped matching cannot report a clean run | `python script/check_name_guards.py` [--self-test] |
| Prove a guard can actually be reached (D73: `run_av_baseline` had its `proc.poll()` check sitting behind an always-true success return, so it never executed, and code like that reads as "already defended" in review). L1 is dead code after a terminator -- the Python counterpart of `unreachable_code` / CS0162, which is why the other two languages need no gate here; L2 is a guard ordered behind a success branch that leaves. `--self-test` transcribes the real pre-fix case as a seed (CI's checkout is shallow, so the seed lives in the file rather than in `git show`) and requires both directions: the D73 shape must fire, the fixed order must stay quiet | `python script/check_unreachable_guards.py` [--self-test] |
| Ask whether an upstream published a newer model than this repository's baseline (GA-13). **Never add it to the gate sequence** -- it needs the network, and a check that only goes green online turns red on sandboxed runners and then gets switched off (D28/D65). The daily `.github/workflows/model-updates.yml` runs it and opens one issue per distinct finding; the baseline is advanced only by an explicit `--record-baseline`, never by the timer, or the alert fires once and stops. Exit 0 requires that at least one comparison happened (D74) | `python script/check_model_updates.py --baseline [--record-baseline]` |
| Parse every tracked YAML/JSON artefact rather than grepping it (D77: a workflow whose heredoc body was written at column 0 was invalid YAML and could never fire, yet every assertion about that file passed, because they read its text and nothing read its structure). Rules are keyed per language on purpose -- a tab is illegal YAML indentation and perfectly legal in TOML. TOML is out of scope and the run says so: importing `tomllib` would make the verdict depend on the host's Python version, and cargo parses it anyway. `--self-test` seeds both directions of every rule; `--files` points it at something not yet staged | `python script/check_artifact_syntax.py` [--self-test] |
| Make every convention name the thing that enforces it (D79: the license-header rule was carried by 121 of 133 source files while the 12 missing ones were all recent additions, because the convention lived only in prose and `CONTRIBUTING.md` never recorded it). Each row carries an evidence grade -- `planted` (a violation was fed to the named checker and it went red; the note must say what was planted), `pointer` (the checker exists and runs, but nobody has tested it against this rule), `asserted` (an acknowledged gap, printed as a count), `human` (undecidable by machine, reason required). Adding a bold item under Conventions/Invariants without a row here fails the gate, and so does a row pointing at a checker that does not exist | `python script/check_convention_coverage.py` [--self-test] |

## 3. Traps that have already cost someone a session

Every one of these was hit in practice. The `D` numbers point into the ledgers.

1. **Never verify with a bare `dotnet test`.** `DomainGlossaryTests` loads
   `Wonslate.UI/translator_engine.dll`, a file `script/build.py` copies there during
   its build step. In a fresh checkout or a new worktree it is absent and three cases
   fail for reasons unrelated to your change. Use `build.py --unit`, which builds the
   native library first. The authoritative entry point is the only one whose verdict
   can be trusted.
2. **The Python suites are enumerated by name, in two places** — `run_ffi_smoke()` in
   `script/build.py` and the `python-ffi` job in `.github/workflows/ci.yml`. This is
   not `unittest discover`: a new `tests/test_*.py` that is not registered in **both**
   silently never runs, locally and in CI.
3. **A test may not depend on the developer's machine.** Three defects were fixed on
   exactly this: fixtures that read host state (D12, now sealed), a repo-root assertion
   that matched the *checkout directory name*, so every worktree and every renamed
   checkout went red on it (D19), and a suite that pinned one spelling of one variable
   (D36). Resolution is `LT_*` > `WONSLATE_*` > bare name, so pinning only
   `WONSLATE_OLLAMA_URL` still loses to a host that exports `LT_OLLAMA_URL` -- the suite
   then drives the developer's model server instead of its own stub. Building a Rust
   integration suite, use `tests/hermetic/mod.rs`: `Spec::new(dir)` pins every spelling of
   every knob it names and *clears* the rest, `apply()` (or `apply_to(cmd)` for a child
   process) writes them, and `verify()` fails by name when the resolved config is not what
   the suite asked for. Never hand-write a `set_var` chain again, and never leave the data
   directory unpinned -- an unpinned `DATA_DIR` reads the developer's `routes.json`, which
   decides routing for you. Assert positions relative to the test file, and resolve both
   sides of the comparison. The cost of leaving a slot unpinned is not only a flaky test: it
   moves the coverage number too, and it can move it *up* -- a file measured against a live
   engine covers branches that no test reaches deterministically, so the flattering figure is
   describing the machine. The measured before/after and the uncovered path this exposed live
   in `REQ-F3`'s coverage rows and `REQ-A2`'s open item.
4. **Glossary seed packs bypass the Rust source-language guard.** Import arrives
   through `tt_glossary_import_pack` and never passes `distill/term_extractor.rs`, so
   a mis-labelled row can still enter the store and the extractor's check will not
   stop it. The gate that does catch it is `tests/test_glossary_hygiene.py` (D20).
   Keep it registered. Judging by *script family* is not enough inside one family: zh,
   ja, ko and yue all resolve to `cjk`, and Han, kana and hangul share that class, so a
   Chinese-authored pack declaring `ja` satisfied every family check (D63, measured:
   the gate printed PASS for exactly such a file). The gate now also bans a sibling's
   exclusive script per row and requires a pack that declares its own pair to show that
   language's script somewhere -- document-level, never per row, because six of the
   fourteen shipped Japanese rows are legitimate kanji-only nouns. `lang.rs` keeps its
   family-only predicate on purpose (Difference 2 in the gate's docstring): at runtime
   the question is "did it write the target language at all", and demanding kana would
   penalise those same kanji-only translations. When adding a language, note that an
   unclassified code is *exempt*, not merely unchecked -- a shipped pack must declare
   only codes the table knows, which `tests/test_glossary_hygiene_rules.py` enforces,
   and the gate's Latin class is deliberately wider than `is_ascii_alphabetic` so
   diacritic-only words (Polish has some) are not read as a wrong script.
5. **A SKIP is not a pass.** When Windows application control blocks the freshly built
   native library, `script/build.py` reports the suites that cross the FFI boundary as
   SKIP instead of FAIL, because the block is an environment condition and not a
   product defect. Say which suites skipped. Never call that run green.
6. **`pipeline::translate_full` has several exits, and each must state its own facts.**
   The domain note and the fallback note describe *the engine that answered*, so an exit
   that re-uses a note computed for another engine is lying: that was D33, present on two
   of the four returns, surviving the S11 wiring and every Rust test until the AI success
   path got its first end-to-end case. If you add an exit, take its notes from the engine
   named in that response's `engine` field, and add a case that reads them back.
7. **When the store keeps two copies of one row, one decision must feed both.** `tm_upsert`
   decides whether a better translation replaces the stored one, and used to hand the
   *incoming* row to the LRU cache regardless of that decision -- a worse candidate for a known
   text therefore left the file and the running session disagreeing, with a restart quietly
   changing the answer back and nothing reported (defect D37, found by chasing a coverage gap
   rather than by a failing test). Any write path must publish what actually settled.
8. **Refuse before writing, not halfway through.** `glossary_import_pack` validated entries
   inside the loop that imported them, so an invalid entry aborted the run *after* its
   predecessors had been upserted: the caller saw `ok:false` and the store kept part of the
   pack (defect D38). A validator that can reject the whole document must stage first.
9. **A new FFI export must be called from `tests/test_phase1_ffi.py`.** That suite records
   which exports it really reached and compares the set against `lib.rs`; a difference fails
   the run. Coverage cannot answer this question -- llvm-cov instruments the binary
   `cargo test` builds, while `lib.rs` is exercised by Python and .NET loading the compiled
   cdylib, so the file reads 0% no matter how much of it is covered. Declaring an export in
   `Engine.__init__` does not count as reaching it; only calling it does.
10. **A restart test needs its own binary.** `TmStore::open` fills a `OnceLock`, so a process
   loads a store exactly once, and every other suite deletes the two JSON files before
   `init`. That is why `tests/tm_reload.rs` exists. Anything asserting what survives a reboot
   belongs in a target of its own; added to an initialised suite it would be testing the
   in-memory index instead, and it would pass.
11. **Write-then-rename is atomic only if the staging name is unique.** The store flushes
   from several threads at once -- a request thread through the dirty counter or
   `tt_shutdown`, the distillation worker through every term it upserts -- and the writer
   used one shared `<file>.tmp` per target, so two flushes renamed each other's staging
   file away: the loser reported a spurious "file not found" for a save the caller had
   already been told about, and a rename winning over a still-writing peer could publish a
   half-written document (defect D39). The name now carries the pid and a counter. Any new
   atomic-write helper must do the same, and must delete its staging file when the rename
   fails.
12. **Anything that reads the environment must resolve through an injected lookup.**
   `OllamaTranslator::new()` and `SidecarTranslator::from_env()` used to call
   `std::env::var` inline, and that cost more than readability: `first_env(...)
   .unwrap_or_else(default)` only runs when the key is *absent*, and
   `.and_then(parse)` only when it is *present*, so which of the two halves of the
   prefix chain executed depended on the machine running the tests (defect D41). Both
   now take `from_lookup(&dyn Fn(&str) -> Option<String>)` and the environment is passed
   in by the caller, so a unit test can drive both directions with pure input and never
   mutate process state. Two rules follow from that shape: cover **both sides** of every
   key (unset must land on the shipped default, set must win in the documented order),
   and let the production constructor read the *same* candidate list the test asserts
   on -- a `#[cfg(test)]` copy of a key list proves nothing about the list the product
   actually reads.
13. **Invisible characters must not leave the sidecar -- but some of them are the text.**
   External audit item 009 measured a Lao translation arriving as `LAO LAO U+200B LAO ...`:
   nothing on screen changes, and every string comparison downstream (dedup, search, cache
   keys, localisation checks, diff scripts) breaks silently. Deleting *all* zero-width
   codepoints is the other bug, and the reporter warned about it from their own false
   positive: the Persian half-space **is** U+200C, Khmer and Myanmar segment words with
   U+200B, and an emoji family sequence carries a real ZWJ. `ct2_sidecar.sanitize_output`
   therefore keeps a mark when the caller's own source text had that codepoint or the
   target script writes with it, and drops everything else -- including bidi controls, a
   soft hyphen, BOM and C0/C1, while keeping `\t\n\r` because line structure is data some
   callers still depend on. It is applied at the HTTP response, not inside each
   `_detokenize`, so every backend and the Rust pipeline (which reads the same bytes
   through `sidecar.rs`) get one answer from one place. Do not "simplify" the whitelist:
   `OutputSanitizationTests` pins both directions, and a mutation of each clause goes red
   on the assertion that needs it.

   Re-measure before you argue about it: `python script/diag_invisible_chars.py both`
   compares what a backend emitted against what left the HTTP port, and asks the ollama tier
   with the product's own prompt. It reports codepoints rather than rendered text -- a Lao or
   Khmer string shows as garbage in most terminals, which is precisely where this class of
   defect hides. Nothing sanitises the model output in Rust today, and that is a measured
   position rather than an oversight: run the tool before adding a second implementation, and
   if it does report noise, reuse the two-rule shape -- a blanket strip repeats the mistake
   item 009's own reporter had already made.
14. **Never hand a multi-line blob to the local checkpoints.** Measured against the real
   MADLAD-400 checkpoint (external item 007): three English sentences joined with newlines
   came back as **one** line of repeated tokens -- `10000000...` followed by fragments of
   the source -- with none of the three translations present, and the HTTP status was 200.
   The transport keeps the newlines and the sentencepiece keeps the `\n` piece, so the loss
   happens in the decoder: nothing in the status code tells a caller its text was thrown
   away. `Handler._translate_lines` therefore splits on newlines, translates each line, and
   rejoins keeping the caller's line count, with blank lines passed through as structure that
   never costs a model call. A line the engine did not answer is named in `failed_lines`
   rather than returned as an empty string (REQ-B2: an unanswered segment must not look like a
   translation of nothing), and a permanently bad target code still fails the whole request as
   422 instead of degrading into partial success (that is D22's rule applied to a batch).
   Batch callers send `{"texts": [...]}` -- one round trip, index-aligned, elements may
   themselves be multi-line -- and `RealMadladLineTests` pins that a batch answer equals the
   same sentences sent one at a time, on the real checkpoint.
15. **A long batch needs a predictable exit, not a smarter watchdog.** External item 004
   measured 130 s -> 280 s -> >900 s for one workload while every call kept returning 200 and
   `/health` kept saying ok, so the caller's "restart on failure" rule could never fire and its
   only recovery was `taskkill` plus a per-language restart loop. Three mechanisms now live here:

   * `--max-requests N` answers N requests and exits **with code 0** and a printed reason. It
     is off by default because the desktop client supervises its own sidecars and an
     unannounced exit would read as a crash there. The request that spends the last permit is
     answered normally and carries `shutting_down` -- an exit the caller cannot predict is the
     same silence the item was filed about, and a non-zero code would teach the supervisor to
     fear the mechanism the caller asked for.
   * `POST /reload` discards the resident checkpoint and keeps the process and the port. It is
     *not* warm by default: re-materialising this checkpoint measured 4.2-5.5 s, while the
     client probes with a 1500 ms HTTP timeout, so a blocking reload would look like an outage.
     Pass `"warm": true` to pay that inside the call.
   * `--decode-slots N` bounds how many decodes may run at once; the overflow is answered
     `429 busy` (with `Retry-After`, `decodes_in_flight`, and a note saying **nothing was
     started**), never queued, and `--queue-wait-ms` is there because waiting is a real
     alternative that had to be measured rather than assumed. It is off by default for two
     measured reasons: at one or two abandoned decodes it makes a patient caller *slower*
     (`Retry-After` is whole seconds while a decode is a few), and a client that does not retry
     trades slowness for a lost translation -- measured at one lost call per burst of three.
     `engine/sidecar.rs` **does** retry it now, inside a budget (`WONSLATE_SIDECAR_RETRY_MS`,
     legacy `LT_*` wins, 12 s by default because the worst `Retry-After` ever measured here is
     5-6 s and one retry was always enough). Three clauses of that wait are pinned and are not
     simplifications: only a 429 whose body says `"error":"busy"` is retried (a permanent 422
     or a foreign 429 is not), a missing/zero/unparsable `Retry-After` means **no retry** rather
     than a guessed interval, and a promised wait is skipped when the whole of it no longer
     fits the remaining budget. A transport failure is a fourth case: an absent sidecar must not
     be dialled once per second for the whole budget. Worst case wall time is deliberately
     `busy_retry + timeout`, because a request that finally gets a slot still gets its own full
     timeout -- and none of this fires at all unless somebody starts the service with
     `--decode-slots`, which is why a non-zero default budget is still zero behaviour change.
     So the switch remains opt-in for callers outside this repo. Because the budget is spent in
     whole `Retry-After` steps and a step is the service's own p50, "is 12 s enough" has no
     answer in seconds: `python tests/test_sidecar_busy_e2e.py --sweep --out-json
     docs/evidence/busy-budget-sweep-madlad.json` prices a grid of (how long the slot is held) x
     (budget) on the real checkpoint. Two readings from the run that wrote that file: a non-zero
     budget **below one step waits 0.00 s** and so behaves exactly like no budget at all -- the
     price of never sleeping half a promise -- and recovery reaches a decode of up to
     `k x Retry-After` for the largest affordable `k` -- in the run that wrote that file the step
     was 7 s for 21 of 24 cells (the first three saw 1 s and 5 s, because the latency window was
     still empty of long decodes: p50 climbed from 0 to 6.49 s), and at that step 12 s bought one
     and reached ~6.2 s of occupancy while 20 s bought two and reached ~9.0 s.
     Page-scale decodes are the other mechanism's job, and that is now measured instead of
     assumed. Two entries, one variable each:

     ```
     python tests/test_sidecar_busy_e2e.py --queue --out-json docs/evidence/queue-wait-madlad.json
     python script/diag_sidecar_batch.py queue --backend madlad --corpus
       script/eval-data/av-zh-en/av-zh-en.tgt --slots 1 --waits 0,10000,30000 --waiters 1,3
       --page-units 6 --out-json docs/evidence/queue-waiters-madlad.json
     ```

     The first runs the shipped Rust client behind a real ~25 s decode and ladders only
     `--queue-wait-ms`; the second arrives behind that same shape with several callers at once.
     Four readings, and a correction to the paragraph above them:
       * **the step is not the decode you are waiting for.** Behind a 25.18 s decode the service
         promised `Retry-After: 2`, because that number is a percentile of the decodes that have
         *finished* (same instant: p50 1.84 s, p99 25.18 s). A budget spent in whole steps
         therefore under-buys a mixed load: 12 s bought six steps and still gave up 10.01 s into
         a 25 s wait. "a step is the service's own p50" reads as "a step is the decode" only while
         every decode is the same size, which is exactly the condition that run satisfied and this
         one breaks.
       * **a queued caller pays no rounding.** The `W 45000` cell recovered in 26.96 s against a
         25.17 s decode in front of it and a 1.89 s decode of its own: -0.06 s of excess. That is
         the difference in kind, not in degree, between the two waiting mechanisms -- a queued
         caller is woken by the release instead of re-asking on a timer.
       * **size it by everything in front of the caller, because an undersized queue is slower
         *and* lost.** `--queue-wait-ms 20000` behind a 26.34 s decode spent the full 20.01 s and
         then lost the translation, while `0` loses in 0.00 s. The service reports the time it held
         each refused caller in `waited_ms`, which is what makes that claim checkable rather than
         inferred (and it agreed with the configured wait in every row of both runs).
       * **exactly-enough is not deterministic.** With three waiters and `W 30000`, one pass served
         3/3 and the next refused the caller whose staircase landed at 31.25 s. A
         `BoundedSemaphore` does not promise first-come-first-served, so a queue is sized with
         margin rather than to the measured second, and which caller loses is not something a
         client can plan around.
     `python script/diag_sidecar_batch.py gate` is what re-measures the service side, and
     `python tests/test_sidecar_busy_e2e.py` is what re-proves the caller side on the real
     checkpoint: it holds the single slot with a real decode, then runs the Rust engine twice
     under the same conditions -- once with the shipped budget, once with
     `LT_SIDECAR_RETRY_MS=0`, which is the pre-existing behaviour -- and asserts the pair, not
     just the success (only the refusal-then-wait episode may recover a translation; the other
     must lose it to demo). Two services running the same checkpoint answer with identical
     bytes, so which one answered is established from `requests_served` deltas, not from the
     text: see rule 17. Re-measuring the gate itself must be
     run **in both setting orders**: per-setting baselines at k=0 differ by up to 1.4 s on this
     host even though nothing is contended there, and the slow slot moved with the order, not
     with the setting. Absolute seconds across settings are therefore not comparable; only
     ratios inside one setting's own baseline are. That is the paired-comparison lesson of
     `load` applied one level up.

   Three rules follow. **One tally**: `/health`'s `requests_served`, `requests_until_exit` and
   the self-exit all read the same counter, so a refused target code cannot be counted by one
   and skipped by another. **A busy refusal is charged to neither**: it is a fact about this
   instant, not work done, so it does not spend a permit and does not enter the latency sample --
   and `decodes_in_flight` counts whether or not a gate is configured, because the number that
   explains item 004's ladder has to be readable on a service that shipped without the gate.
   **Never invent a countdown**: `requests_until_exit` is `null` when
   unbounded. **Never promise reclaimed memory in prose**: `reload` reports what it discarded,
   and `python script/diag_sidecar_batch.py reload` is what re-measures whether the bytes came
   back and what the reset cost -- reporting the calls that merely translate *separately* from
   the one call that pays for the reset, because averaging them blames the reload for the
   seconds it buys on purpose. Measure a batch the way the report described one: `--corpus`
   with distinct sentences, compared **pass against pass**, because a ladder is one body of
   work done again. A run that repeats a single sentence is refused rather than reported -- its
   ratio is flat for the wrong reason, and a flat number measured that way is what gets quoted
   later as "no drift over hundreds of calls". `RealMadladReloadTests` pins the part no stub can
   prove: after a reload on the real checkpoint the service still answers, and says it is cold
   in between.

   The retention line this all feeds (defect D30 item 4) is one number: **the last complete
   pass must not cost more than 1.15x the first**, reported by `pass_report` and carried out as
   the exit code. 1.15 is not new -- it is what `run_rotate` already uses to call a revisited
   language slower, and the two places must not drift into meaning different things. Three
   cautions for whoever reads the output:

   * **Paired or nothing.** A head-vs-tail window over a varied corpus compares *different
     sentences*, and it disagreed with the paired number by 8% on the run that set this line
     (`report` now prints UNPAIRED next to its own ratio). Quote the pass report.
   * **Complete passes only.** A run that stopped mid-pass did different amounts of work, so
     those passes are excluded; if two full passes are left, the tool says it cannot decide.
   * **A single repeated sentence proves nothing.** `--text` alone now refuses to state a line
     rather than print a flat ratio, because its work really is identical -- that flat number is
     exactly what gets misquoted later as "no drift over hundreds of calls".
   * **Never compare halves of the same lap.** Over a varied corpus every lap's second half read
     slower than its first (1.022-1.083 here) because the sentence order repeats, so the later
     sentences are simply the costlier ones -- five times in a row, with no accumulation across
     laps. A window inside one lap is the unpaired mistake again, and 1.083 is uncomfortably
     close to the limit to leave in the output unlabelled.
   * **Keep your own work out of the measurement window.** Running test suites while the batch
     runs inflates the early laps, and it does so in the direction that makes the verdict pass
     (a busier baseline makes every later lap look faster). Either run nothing else, or re-compute
     the judgement on the undisturbed laps only -- `docs/evidence/retention-madlad-en-zh-1500.json`
     carries that `clean_segment_check` beside the headline number for exactly this reason.

   The measurement that set the line, including what it does not show, is in
   `docs/evidence/retention-madlad-en-zh-900.json` and the 1500-call follow-up beside it
   (`-1500.json` for the reading, `-1500-calls.json` for the per-call curve, so the aggregates
   can be re-derived rather than trusted). Re-measure with the command in the table
   above rather than re-quoting it, and keep the environment precondition with the number --
   this host's long-lived sidecars are named in those files for exactly that reason.

16. **A caller that hung up is a fact to record, not a crash to print.** `ThreadingHTTPServer`
    gives every request its own thread and `socketserver` prints a full traceback for any
    exception that escapes a handler -- which on a batch where clients time out means one stack
    trace per abandoned call. Measured: **47 tracebacks, 127 KB of stderr, in a single run**, and
    every one of them was the service finishing an answer and finding nobody listening. That is
    also why the same event was mis-attributed twice while it was being investigated: the noise
    looked like information, so the numbers were read off it ("47 of the 60 abandoned calls", and
    "the other 11 must be a post-handler flush" -- wrong both times; splitting by client port and
    stack shape gave 36 answers and 11 refusals, all inside `_send`).

    `Handler._send` therefore catches `ConnectionError` and nothing wider, and counts the two
    outcomes apart: an *answer* written to a gone caller spent a decode, a *refusal* spent
    nothing. `OSError(ENOSPC)`, a `ValueError`, a crashing engine all still reach the log, and
    `GoneClientTests` pins each of those boundaries -- including the one that matters most, that
    overriding `handle_error` is not an acceptable fix. Two places in this repo used to do
    exactly that to keep their output tidy (`AbandonedRequestTests` and the probe's `race` mode),
    which is how this sat unnoticed: a silenced log is not a fixed log. The `race` override stays
    on purpose and says why -- it runs a *checked-out revision* that predates the accounting, and
    silencing somebody else's stderr is not the same as hiding your own.

    What the counts are **not**: a duration. `writes_to_gone_clients()` answers "how many answers
    went nowhere"; the seconds thrown away are measured by
    `python script/diag_sidecar_batch.py abandon`, which prints both. Neither number is on
    `/health` yet -- publishing a service's own waste is an open decision, and a field that
    appears in a contract by accident is hard to take back out.

17. **A new environment knob must enter the hermetic table, and a suite must pin every slot it
    could dial.** `tests/hermetic/mod.rs::KNOBS` is the one list of knobs an integration suite
    pins or clears, and `tests/env_pin_coverage.rs` compares it against every `LT_*` /
    `WONSLATE_*` name in `src/` and fails the build when they disagree. The gate exists because
    of **D45**: the caller-side wait budget shipped with behaviour, unit tests, public docs and a
    decision record, and nothing noticed it was missing from the table -- one export in a shell
    would have decided how long every Rust suite may wait, on a machine CI does not have, both
    green. The same class then caught the suite that measured that feature: its first draft
    pinned `LT_ARGOS_URL` and the AI slot but **not `LT_MADLAD_URL`**, so the Rust engine
    translated against the developer's long-lived **ungated** sidecar on 11436 while the gated
    child under test only ever saw probe traffic. Every "the wait recovered the translation"
    assertion passed -- against a service that had no gate at all. Three rules follow.

    * Pin **both spellings of every network slot** a suite could reach, including the one you
      think it does not use; the prefix chain does not care what a suite meant.
    * Where a claim depends on **which** service answered, prove it from the service's own
      counters (`requests_served` deltas, `decodes_in_flight`) rather than from the answer text:
      two MADLAD instances return byte-identical translations, so the text cannot tell them
      apart, while a served-request delta can also show that the engine's own request was
      refused rather than never gated.
    * A script-style suite must skip **exactly** the names it would have passed. Reporting one
      name twice makes the CI total disagree with the local one for a reason that has nothing to
      do with the product (this suite's skip path did that on the way out).

18. **A wait knob needs an upper bound, and a percentile describes finished work -- not the
    request in flight.** Three lessons from three rounds, all measured rather than argued:

    * `--queue-wait-ms` shipped with no ceiling. The mutation meant to prove the new suite could
      see a unit error (`/1000` written as `*1000`) turned out to be a **real defect, D46**: past
      CPython's limit a timed `Semaphore.acquire` raises `OverflowError`, the exception escapes the
      handler, and the caller's connection closes with no response at all -- so the one setting
      meant to be *more* patient than a refusal was the least honest thing the service could do
      (this host turns over between 1,000,000 s and 20,000,000 s). Fixed with two guards: the
      resolver clamps to `QUEUE_WAIT_CEILING_S`, and the handler answers a wait it cannot represent
      as a refusal. Each has its own test row, because the second is reachable only when the first
      is bypassed -- one shared test would let anyone delete the guard unnoticed.
    * `Retry-After` is a percentile of the decodes that have **completed**. Behind a 25.18 s decode
      the service promised `2`, because the window then held a 1.84 s sample while p99 was 25.18 s.
      A caller that sizes itself by that promise under-waits any mixed workload; the queue is for
      those, and a document that quotes a percentile has to say which one and of what.
    * A caller *inside* a queue was invisible on `/health` (**D47**, 2026-10-11): `acquire(timeout=W)`
      blocks before `decode_started()` is called (slot not yet granted, so `decodes_in_flight` is
      still the old value) and before `record_served()` runs (no decode yet, so `requests_served`
      doesn't count it either). The sizing rule W ≥ sum of decodes ahead of a caller requires
      exactly that number, so an operator watching `/health` had nothing to evaluate it against.
      Fixed with `queue_entered/queue_left` wrapping only the blocking section (guarded by
      `queue_wait_s > 0` so a zero-wait immediate refusal does not become a phantom queue entry),
      exposed as `decodes_queued`. The mutation proof must delete both halves: removing
      `queue_entered()` hides the caller; removing `queue_left()` leaks a permanent phantom.
    * A server-side **queue hold is invisible to the retry budget**. A caller parked in
      `acquire_decode` gets no `429` -- the handler writes nothing while it waits -- so the
      `*_SIDECAR_RETRY_MS` budget never engages for a queued request and cannot rescue it. The
      only thing that bounds such a call is the **per-attempt** `*_SIDECAR_TIMEOUT_MS` (30000 ms
      default), which `tcp_roundtrip` applies as a socket read timeout. Locked by the pair
      `a_server_hold_longer/shorter_than_the_timeout_*`, each killed by breaking the timeout in
      the opposite direction -- a single one-sided test would pass whether the inequality held or
      not.
    * **Correction the same day, then fixed (I under-counted the desktop chain).** The bullet
      above framed the desktop combo as `*_SIDECAR_TIMEOUT_MS >= W + decode` and treated the .NET
      1500 ms / 5000 ms values as the only outer layers. Both were wrong: a desktop `/translate`
      crosses **four** timeouts, and the outermost is `MainViewModel.TranslateTimeout`, a UI
      logical timeout that wraps the whole `tt_translate_full` FFI call and fires **before** the
      Rust 30 s -- so the Rust-side inequality is necessary but not sufficient for the desktop.
      It shipped at a hardcoded 20 s with `TranslateRetries = 1`, and that retry re-issued a whole
      decode while the abandoned one was still running: with the gate off by default the two
      overlap and slow the work still wanted (the D30 item-2 amplification, measured 2.5x), which
      a multi-line paste hits easily because the sidecar decodes lines **sequentially on one slot**
      (sum of lines; a measured 110-word madlad decode is ~25 s). **The owner's decision, now
      implemented:** the default was raised to **45 s** -- chosen because it sits above the Rust
      client's own worst case (`busy_retry 12 s + timeout 30 s = 42 s`), so the UI no longer
      abandons a call the engine is legitimately waiting out -- made env-overridable via
      `*_UI_TRANSLATE_TIMEOUT_MS` (the `LT_` > `WONSLATE_` chain, read on the .NET side, injected
      in tests so they never depend on the host env), and the auto-retry default set to **0** (the
      mechanism stays, pinned to 1 by two unit tests). This was not a silent defect: the 20 s had
      honestly reported "翻译超时（20s）" (REQ-B2 intact); the change was a UX call, made by the
      owner. The pair of `a_server_hold_*` hermetic tests still pins the Rust->sidecar segment --
      real, but not the whole chain.

    The round also carries a method note: a mutation round can find a defect instead of confirming
    a suite, which is the cheapest way one has ever paid for itself here.

19. **A write endpoint must report the write it actually did.** `tt_glossary_upsert` shared the
    machine path's "replace only if strictly higher" rule, so correcting a term you had just saved
    changed nothing: the row kept the first value, only `frequency` moved, and the ack answered
    `{"ok":true}` while the UI printed "saved" (defect **D49**, measured on the released binary
    across the FFI, and `docs/sdk.md` was promising "confidence is the caller's" at the same time).
    Two doors, two rules now. A user edit (`tm::glossary_upsert_manual`) always lands, stamps
    provenance `manual`, and may set / keep / clear `user_locked` -- that flag previously had no
    entry point and no way to be read back at all (**D35**), so listings now carry it per row. A
    machine write (distillation, seed-pack import) still needs a strictly higher confidence and
    still stops at a locked row, but it reports `written` / `skipped_locked` / `skipped_not_higher`
    instead of one flattering total (`imported` keeps its old meaning: entries in the pack, so a
    released installer still reads it), and `tt_glossary_delete` reports `removed`. The rule to
    keep: whenever a call can be accepted and still change nothing, the response must say which of
    the two happened -- `ok` is about the request, not about the store. A lock is a shield against
    machines only: a locked row must stay editable by the person who locked it, or the feature
    recreates D49 with the labels swapped. Deletes go through the full primary key now: a
    seed-pack row is removable by `tt_glossary_delete_with_domain` (D50), while the original
    three-argument `tt_glossary_delete` keeps addressing the generic row -- widening an existing
    export's arity was measured to be worse than adding one, because a caller compiled against
    the old signature gets no error at all, just a plausible answer aimed at another row.
20. **A repeat is only a repeat if it measured against the same engine build.** `--run-index`
    appends into one evidence file and `--resume` keeps the earlier runs in it, so a rebuilt
    DLL puts two different measurements inside what looks like one arm's noise series -- and
    max-minus-min across two builds measures the *builds*, not sampling. Every run therefore
    carries `generated_at` plus `engine_build_sha` / `engine_build_source` (defect D70, found
    in S13 T3). `tt_version()` cannot serve: it returns `CARGO_PKG_VERSION`, which is `0.1.0`
    for every rebuild of the same checkout. Keep "no id recorded" distinct from "same id" --
    `_spread()` in `script/run_av_baseline.py` takes a floor from one build only, and
    `_status()` names the three different reasons a floor is missing rather than printing one
    sentence for all of them, which is D57/D58's rule applied to this column.

21. **A check's "nothing is wrong" answer must be earned by looking at something.** Defects
    D74/D75/D76 all came from `script/check_model_updates.py` while wiring the scheduled run, and
    each is a version of the same mistake: `unrecorded` and `incomparable` rows used to fall into
    exit code 0 with the message "no newer upstream version found", so a machine with nothing
    installed (which is every CI runner) reported an all-clear it had not measured -- a timer built
    on that says "fine" every day forever. So `verdict()` returns 0 only when at least one
    comparison actually happened, names what was *not* compared on the same line when some assets
    were skipped, and treats "upstream publishes no digest for this asset" as its own answer rather
    than as agreement. Two further rules from the same run: **an unexercised URL or field name is
    an unverified one** -- Hugging Face spells its sha256 `lfs.oid` (reading `sha256` made every
    healthy install look unverifiable) and sherpa-onnx's model archives live under release *tags*
    (`tts-models`, `asr-models`), not under `/releases/latest`, which contains no archives at all;
    both were caught only by calling for real, after tests had passed for a day on fixtures that
    matched the code's assumptions instead of the upstream's responses. And a credential read from
    the environment must be **said out loud** and sent to exactly one host (`GITHUB_TOKEN` goes to
    `api.github.com` only -- `tests/test_model_updates.py` asserts the other probes never receive
    it), because a host-exported variable that silently changes a result is unreproducible in the
    way D45 describes.

22. **A convention that only lives in prose stops being followed -- and a rule that matches
    prose *about* the rule is not a rule.** Both halves came from the same round (2026-10-17). The
    SPDX/Copyright header is carried by 121 of 133 source files here: every `.rs`, `.cs`, `.toml`
    and `.xaml` file has it. The 12 that did not were all created by recent work, nine of them
    across four consecutive rounds by the same hand, and nothing noticed, because no check
    existed. The header is now check 3 of `tests/test_source_lint.py` and the debt is paid off.
    The way that check first failed is the more general lesson: it exempted a file whenever the
    text `lint-allow: no-header` appeared anywhere in it, so the first file it examined -- the
    linter documenting that marker -- was granted the exemption it describes (defect **D78**).
    An exemption must be declared, on its own comment line at the top of the file, and a file the
    checker cannot open must be a counted outcome rather than a pass: the first draft printed
    "1 file(s) carry the header" about a file that was not on disk at all.

23. **Every convention must name its enforcer, in one of three honest grades.** Defect D79 is
    D78's quieter sibling: the repository requires a license header on every source file, 121 of 133
    files had one, and the twelve that did not were all created by recent work -- nine of them across
    four consecutive rounds. Nothing noticed, because the rule lived in prose and `CONTRIBUTING.md`
    never recorded it either. `script/check_convention_coverage.py` now holds the contract: each
    convention lists `enforced_by` and an evidence grade -- `planted` (a violation was fed to that
    checker and it went red), `pointer` (the checker exists and runs, untested against this rule),
    `asserted` (nobody checks it -- an admitted gap, printed as a count), plus `human` for rules no
    machine can decide. Do not upgrade a grade to look tidy: `planted` requires naming what was
    planted, and an `asserted` row may not name an enforcer. Two findings came out of writing it and
    both are worth keeping: a convention the gate flagged as ownerless turned out to be enforced by
    three Rust unit tests that give both spellings of one knob *different* values
    (`lt_data_dir_wins_over_wonslate_for_compat`) -- so a GAP report means **go look**, not
    "declare it unenforced"; and `Line endings` is enforced by git itself (`* text=auto eol=lf`,
    verified by staging a CRLF file and reading the index blob: zero CRs), which corrects earlier
    rounds that recorded work-tree CRLF as a delivery defect when it never reaches the repository.

24. **A digest proves *which* artifact, never *all of it* -- and an offline stub proves only my
    assumptions.** Defect D80: `net_guard.fetch` ended its read loop on an empty chunk and renamed
    the file into place. With a pin the checksum usually caught that by accident; under
    `--allow-unpinned` (which `sense-voice` uses) a body truncated at 40 % landed as a successful
    download, and every failure deleted its partial, so a gigabyte asset restarted from zero on
    each of its four attempts. Two rules follow. (a) Length is its own check: whatever the server
    promises in `Content-Length` / `Content-Range` must equal the bytes actually read, and a
    transport failure must be distinguishable from a content refusal -- `TruncatedError` keeps the
    bytes, `GuardError` throws them away, and the retry loops' refusal keyword lists are pinned by
    a test so a truncation cannot be mistaken for a refusal. (b) Before trusting a resume rule,
    **measure the real upstreams**: on 2026-10-18 huggingface.co sent neither `Content-Length` nor
    `Content-Range` even on a 206, and jsDelivr chunk-encodes the index -- both answer a bare 206.
    My stub had assumed a well-behaved slice answer, so a rule written against the stub would have
    spliced two generations on the unpinned path in production. Hence: append to a partial only
    when the claim file names this URL and this pin, and refuse an unverifiable 206 slice when no
    digest can prove the result. Concurrency is settled by an atomic rename of `.part` (the claimer
    resumes, the loser downloads its own copy) rather than by a pid table or an mtime heuristic.

25. **A record's key must be the same granularity as the thing it describes.** Defect D81:
    `script/model_origin.py` wrote one `wonslate-origin.json` per directory, and
    `fetch_madlad_model.py` installs four assets into one directory -- so the fourth write replaced
    the first three and the surviving record claimed the directory *was* `config.json`, which
    `read()` confirmed as `ok`. `check_model_updates.py` then compared the 2.9 GB checkpoint's
    upstream oid against a small JSON file's digest. The record is now keyed per asset inside one
    file (`{"format": 2, "assets": {...}}`), and the reader must name the asset: several candidates
    with no name given is answered `ambiguous`, and an un-recorded name is answered `absent` --
    never a pick, never a neighbour. Legacy single-object files stay readable (they exist on
    installed machines) and merge into the new format. Two related rules paid for in the same round:
    **do not send `If-None-Match` together with `Range`** ("this slice is current" and "the whole
    file is current" would arrive indistinguishable), and **a 304 is not proof about our own copy**
    -- `--force` re-verifies the local bytes against the recorded digest and falls back to a plain
    fetch when they differ. Verified against the real server: GitHub answered a genuine 304 and
    urllib raised it as `HTTPError` here, so the empty-response shape is covered by stubs only --
    say so rather than implying both were seen live.

## 4. Conventions and invariants

**This file is not the source of truth for either.** `CONTRIBUTING.md` is: comment
language, Conventional Commits, line endings, UTF-8 marshalling across the FFI
boundary, the `LT_*` > `WONSLATE_*` > bare environment prefix chain, and the two
invariants that must not be broken — the privacy fail-closed clamp in `pipeline.rs`
and the Apache-2.0 / MIT licensing gates. If a convention needs changing, change it
there, not here.

## 5. Layout map

| Path | Owns | Read first, because |
|---|---|---|
| `translator-engine/src/lib.rs` | the C ABI export surface and the panic guard | `docs/sdk.md` — the exported set is a frozen contract |
| `translator-engine/src/pipeline.rs` | the five-layer pipeline, privacy clamp, honesty notes | `CONTRIBUTING.md` §Invariants |
| `translator-engine/src/router.rs` | rule-based engine selection and the upgrade path | `pipeline.rs` — the clamp runs *after* routing |
| `translator-engine/src/engine/` | `Translator` trait, registry, demo/http/ollama/sidecar adapters | `engine/mod.rs` — an unregistered engine is unreachable |
| `translator-engine/src/tm/` | translation memory: persistence, LRU, flag-bad | `docs/terminology.md` |
| `translator-engine/src/distill/` | term extraction and its scoring thresholds | §3.4 and `tests/test_glossary_hygiene.py` |
| `translator-engine/src/config.rs` | four-level config precedence (also `lang.rs`, `glossary.rs`, `confidence.rs`) | `docs/sdk.md` — the config contract |
| `translator-engine/src/license_gate.rs` | `REGISTERED_DEPS`, the allow-list for non-crate deps | `CONTRIBUTING.md` §Invariants |
| `translator-engine/src/bin/wonslate-mcp.rs` | the MCP stdio server over the same core | `docs/mcp.md` |
| `Wonslate.UI/` | `Interop/` mirrors `lib.rs`; `Sidecar/` owns the process and tells liveness from readiness; `ViewModels/` is the app logic; `Audio/` is the VAD/ASR/TTS chain | change both sides of the FFI together; `docs/user-guide.md` for voice |
| `sidecar/ct2_sidecar.py` | the CT2 Argos / MADLAD HTTP service | `docs/sdk.md` §sidecar; the CT2 backends do not consume `glossary` |
| `script/` + `tests/` | the authoritative build/test entry, the benches, and the gate suites | §2, §3.2 and `.github/workflows/ci.yml` |

## 6. Verify before you claim done

1. `python script/build.py --test` from the repo root, and quote the last line you got.
2. Anything touching a quality claim: re-run the named bench and point at the evidence
   file (`docs/translation-benchmark.md`, `docs/flores-benchmark.md`).
3. Anything adding a suite, a gate or an engine: update both enumerations (§3.2), and
   `docs/sdk.md` if the export surface moved.
4. Do not write project status numbers into this repo's docs. They belong to the
   private ledgers, and here they would only go stale.

## 7. Where truth lives, and what is open right now

Status truth is `docs/requirements/00-需求总纲.md` and `docs/tasks/00-任务总纲.md` in
the private umbrella repo. The open items worth knowing before picking a task — the
identifiers are stable anchors, the detail lives in the ledgers:

- `D30` — closed on 2026-10-11. `/health` reports `requests_served`, a bounded latency window,
  `rss_bytes`, the in-flight and queued decode counts, and the work spent on callers who already
  hung up (`answers_to_gone_clients` / `refusals_to_gone_clients`, cumulative), and the answering
  process's own `pid` (**D72**, 2026-10-15): a caller that starts this service has to be able to
  prove the instance replying is the one it started -- reserving a free port, releasing it, and
  handing the number to a child leaves a window in which another process binds it, and then every
  counter is read from the wrong service while `/health` still answers `ok`. A caller that gets no
  `pid` back must treat the instance as unidentified, not as its own. A bounded batch
  (`--max-requests`) and a reload that is not a restart (`POST /reload`) exist, the retention line
  is measured, and both remaining decisions were made by the product owner: `--decode-slots` ships
  **off** by default (low dose costs more; callers outside this repo do not retry), and the waste
  counters go on `/health`. What is left is a known structural limit, not work: an in-flight CT2
  decode cannot be cheaply interrupted over HTTP/1.1.
- `D34` — closed 2026-10-08. A pack row that omitted `confidence` entered at the reader's
  0.90 default rather than the intended 1.0, so a seed pack could not displace the distilled
  rows it was written to override while still reporting `imported: N`. The stamp is now
  presence-based. Rows already in a user's store keep their old value until the pack is
  re-imported; the AV comparison was re-run after that re-import and moved only inside the
  repeat-to-repeat spread, so the published conclusion is unchanged.
- `D35` — closed 2026-10-12. `user_locked` now has an entry point and a read path: an optional
  key on `tt_glossary_upsert` (set / keep / clear), the flag travels with every listed row, and
  the desktop glossary tab has 锁定 / 解锁 with a lock column. Editing a term through the UI locks
  it; locking never blocks the user's own next edit (see rule 19).
- `D49` — closed 2026-10-12 (same round as D35, same code site): a user edit at equal confidence
  silently replaced nothing while the ack said `ok`. See rule 19 for the two-door rule that
  replaced it and for the accounting the write endpoints now give back.
- `D50` — closed 2026-10-12. The delete key now includes the domain
  (`tt_glossary_delete_with_domain`, the 19th export). The 3-argument export keeps its exact
  meaning -- generic row only, `removed:false` when nothing matched -- because the frozen
  contract allows adding but never changing, and an arity change fails no caller loudly.
- `REQ-B2` — scope decided 2026-10-11: the no-silent-degradation invariant now covers the
  sidecar's public HTTP channel as well as the Rust pipeline (008 followed as D48).
- `S12` — closed as **a small but statistically supported gain**, once D32 was fixed and both arms were
  rerun under input-aware injection. The earlier "no attributable gain" reading was an artifact of
  injecting a pack prefix rather than the terms each sentence uses: a measurement hole can erase a
  real effect as easily as a scoring bug can invent one. Disclose it as terminology consistency plus
  a modest quality edge, never as "much better translation" — the polysemant subset came out
  slightly negative. T2 (more pairs) and T4 (larger corpus) remain.
- `N-10` — microphone capture is missing; the voice chain runs on files only.
- `F4` — cross-platform has not started; the client is Windows-only today.
- `REQ-F5` — MCP covers the stdio server; SDK, Resources and authentication are open.
