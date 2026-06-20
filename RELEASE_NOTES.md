# v0.1.0 — scrape_new offline audit baseline

**Date:** 2026-06-20
**Tag:** `v0.1.0`
**Commit:** `28f5d23`

First stable baseline. Everything after this tag is forward work on
"real user closure"; everything before it is the **green floor** that
regressions are measured against.

## At a glance

| | |
|---|---|
| Tests | **461 passed** on local + CI |
| CI matrix | `windows-latest` × `ubuntu-latest` × `Python 3.10` × `Python 3.11` (4 jobs, all green) |
| Python | 3.10+ |
| Platforms | Windows / Ubuntu / macOS (CI covers win + linux) |
| License | MIT |
| Repo | https://github.com/L1GOAT/Learning-Resources-Download-Scrapes |

## What's in this baseline

### Download pipeline

- `scrape_new/workflows/{chaoxing,xuetangx,zhihuishu,icourse163}` — 4 platform one-shot downloads
- `--scan-only` / `--max-tabs` / `--resume` / `--retry-downloads` / `--verify-resume-only`
- `_chapter_tree.json` / `_resource_naming_manifest.json` / `_review.html` / `_retry_downloads.json`
- `resource_key` / `download_resume` / `cldisk` & `ananas` Referer fixes
- video vs document classification (videos in `视频/`, PPT/PDF/DOC in `文档/`)

### Upload pipeline (plan-first)

- `build-mapping` — outline.json + videos → `_mapping.json`
- `upload upload` defaults to **plan-first**: writes `_upload_plan.json` / `.md` instead of touching the backend
- `upload apply-plan` validates `course_id` + `mapping_hash` + `tree_fingerprint` + `scope` (all four must match)
- `--only-lessons` / `--only-resources` local edits **forbid** `--reset-confirm`
- `RENAME` defaults to `pending`; needs `--confirm-rename`
- reset / rename require explicit `--reset-confirm <course_id>`

### Audit layer

- `scrape_new audit` subcommand — pure local, no network
- `classify_resource_role()` — extension / MIME / title / tab evidence chain with confidence
- `audit_scan_completeness()` — empty lessons / chapters / count mismatch / duplicate objectids
- `audit_mapping_alignment()` — missing local files / unused downloads / attachment-as-video / duplicate file use / PPT-only informational
- `write_resource_audit_reports()` — `_resource_audit.{json,md,csv}`
- Chinese phrases for non-English audiences: `可能漏扫` / `需要人工确认` / `可以安全跳过` / `建议补资源` / `建议只重扫该节`

### Wizard / assistant

- `python -m scrape_new wizard` (alias `assistant`)
- 7 intents: `download` / `scan` / `build_mapping` / `upload` / `retry` / `modify` / `audit`
- 4 cookie sources: `curl` / `string` / `file` / `env`
- `WorkflowPlan` JSON / Markdown output — GUI-ready
- `--execute-step <id>` — runs a **non-dangerous** step via `subprocess.run(shell=False)`
- Dangerous / `requires_confirmation` steps are **always refused**, command printed for manual copy
- `_wizard_runs.jsonl` — append-only audit log

### Offline fixture (no real network needed)

- `scrape_new/tests/fixtures/course_audit_demo/` — 2 chapters / 4 lessons / 7 intentional issues
- `scrape_new/docs/examples/offline_e2e_workflow.md` — 4-step walkthrough
- `scrape_new/docs/examples/resource_audit_demo.md` — sample audit report

### Engineering

- `.github/workflows/ci.yml` — 4-job matrix, `cache: pip`, concurrency cancel
- `SECURITY.md` — forbidden content list / leak response protocol / report channel
- `CONTRIBUTING.md` — dev setup / contribution rules / commit style
- `.gitignore` whitelist: only `scrape_new/` + `tests/` + repo metadata tracked
- README CI badge

## How to use it

```bash
# Clone + install
git clone https://github.com/L1GOAT/Learning-Resources-Download-Scrapes.git
cd Learning-Resources-Download-Scrapes
pip install -r scrape_new/requirements.txt
pip install pytest

# Run the offline fixture (no real network)
python -m scrape_new audit \
  --chapter-tree scrape_new/tests/fixtures/course_audit_demo/_chapter_tree.json \
  --manifest scrape_new/tests/fixtures/course_audit_demo/_resource_naming_manifest.json \
  --mapping scrape_new/tests/fixtures/course_audit_demo/_mapping.json \
  --output-dir ./demo_output

# Run the test suite
python -m pytest scrape_new/tests -q
```

## Known limitations

- `scrape_new/tests/test_real_course_e2e.py` and `test_scan_chaoxing.py` reference a local "物理化学" real-course fixture via `pytest.skip` — the tests are local-only and skip on CI. They will be reworked in the next phase (issue #2).
- `python -m pip install -e .` not used by CI; package installed via `requirements.txt` only.
- `requires-python` in `pyproject.toml` says `>=3.11` but `setup-python` matrix covers 3.10 too — discrepancy to fix in next phase.

## Next phase (issues to be filed)

- `feat: add end-to-end real course download report`
- `refactor: centralize project root and fixture path helpers`
- `docs: add user guide for scan/audit/execute workflow`

---

This tag is the **green floor** — anything that breaks these 461 tests is a regression.