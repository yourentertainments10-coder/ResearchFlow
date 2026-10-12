# Design: the answer contract and the number lock (Phase 1 of the general research engine)

Status: implemented in `src/sie/answer/` (ADR-041). Approved by the owner on 2026-10-11 after the architecture proposal that followed `GENERALISATION_AUDIT.md`.

## 1. Requirement
Whatever later produces an answer (a deterministic operation, a web source, a model), the answer must be built from the same parts: layered statements, figures that name their source, cited evidence, disclosed gaps. A number may not appear in an answer unless it came from a registered figure. An answer that breaks a rule is never shown.

## 2. Assumptions
1. Library only: no command, no table, no network, no new dependency (pydantic and the standard library).
2. It does not depend on `sie.datasets` (still in flight); it reads results by duck typing and has an adapter-ready shape.
3. Nothing existing imports it yet (a test enforces that), so production behaviour cannot change.
4. Language: the number-word check is English. Digits and other numeric characters are caught in any script.

## 3. Parts (`models.py`)
| Part | Meaning |
|------|---------|
| `Figure` | One exact number (`Decimal`, never a float), display decimals, unit, label, `source_kind` (database, operation, user_file, evidence), `source_ref` (result hash, query id, file hash or evidence id), provenance |
| `EvidenceRef` | An outside source: URL, title, retrieval time (time zone required), tier 1 to 5, a short quote. Shape only; nothing fetches it in this phase |
| `Gap` | Something missing, partial, excluded, stale, in conflict, unverified or unsupported |
| `Statement` | One claim in one `Layer`; numbers only as `{F:figure_id}` placeholders; optional `declared_literals` |
| `Answer` / `Refusal` | The whole answer, or "cannot answer" with a code. A refusal is a result, not a failure |

Layers (`PRODUCT_VISION.md` section 6): verified fact, derived analysis, interpretation, external evidence, limitation.

## 4. The rules (`verify.py`)
1. **Number lock.** After the `{F:id}` placeholders are removed, a statement may contain no digit run, no Unicode numeric character (fractions, superscripts, Roman and CJK numerals) and no English number word, ordinal or multiplier ("three", "first", "half", "twice", "dozen"). Anything written on purpose must be listed in `declared_literals`, must really occur in the text, and is part of the canonical output so an audit sees it. Rendering uses only the figure's fixed format (half-up rounding, comma thousands, unit after the number).
2. **Layer and source.** Verified fact cites database figures only. Derived analysis cites operation or user-file figures. Interpretation cites database, operation or user-file figures and must cite at least one (it adds no number). External evidence cites evidence figures and at least one evidence item. Only external evidence may cite evidence. Limitation may cite anything.
3. **No orphans.** Every placeholder, evidence id and gap id resolves; ids are unique; a figure that is never shown, and an evidence figure whose evidence is missing, are rejected.
4. **Disclosure.** Every gap must be referenced by a limitation statement, and every answer has at least one limitation.
5. **Fail closed.** `render_markdown` verifies first and raises `AnswerRejected`. Output is deterministic: the same answer gives the same canonical JSON, SHA-256 and Markdown.

## 5. What the lock does not do (stated limits)
- It checks numbers, not names or truth: a statement can still name the wrong country. Entity checks need the router and ledger of later phases.
- It cannot see numbers in other languages' words. A model that writes "tres" would pass; the planned model layer (Phase 5) must therefore only fill placeholders, never free numbers.
- A declared literal is trusted as declared. It is visible, countable and testable ("no declared literals in model output" can become a rule later).
- A figure is only as good as its source: the contract records where it came from, it does not recompute it.

## 6. Adapters (`adapters.py`)
`figure_from_cell(table, where=..., column=...)` takes one cell of one row (no match or several is an error). `figure_from_value` converts ints, numpy scalars and floats (15 significant digits, as `sie.datasets`); NaN, infinity, missing and booleans raise `FigureError`: a gap is never filled. `operation_provenance(result)` reads `provenance.result_sha256`, `operation`, `parameters`, `input_sha256` from a `sie.datasets`-style result.

## 7. Tests (`tests/unit/test_answer_contract.py`, 41)
A full answer built from the real `metrics.country_summary`; the lock against typed numbers in digits, words, ordinals, Arabic-Indic and Devanagari digits, fractions, superscripts, Roman and CJK numerals; a mutation test that replaces each placeholder by its rendered number and expects a violation; layer, orphan, disclosure and refusal rules; determinism and hash stability; formatting; adapters; and three regression guards: the package imports nothing from the sports, database or `sie.datasets` code, uses no dependency beyond pydantic and the standard library, and no existing module imports it.

## 8. Next
Phase 2 (ledger tables and uploads in `raw_documents`) is designed against this contract; it needs its own proposal and a migration. Wiring `sie.datasets` results into `Answer` happens when that package is merged.
