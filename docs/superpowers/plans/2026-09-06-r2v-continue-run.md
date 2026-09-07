# r2v「续运行」Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 r2v 批量面板新增「续运行」按钮——只采样勾选组、但按时间轴顺序把全部组（新采 + 缓存回填）合并成单一 `merged.mp4`，并修复部分运行时未勾选组预览被清空导致「原片段看不到」的缺陷。

**Architecture:** 前端新增 `continueRun` 状态（与 `runSelectEnabled`/`mergeOnly` 平级，蕴含 `runSelectEnabled=true`），按钮走时间轴工具栏（r2v 用 fl2v 风格时间轴勾选框）；payload 增补 `continueRun`。后端 `DirectorPlan.continue_run` 放宽缓存回填门控（不再只在 `export=all`），保证全时间轴有序合并，缺缓存只显著报告不补采。核心 bug 修复：`executing` 事件只清「将被重采」段的预览。

**Tech Stack:** 原生 JS（ComfyUI 前端扩展，ES modules）+ Python（ComfyUI 节点后端）。无测试框架：后端用 `python -m py_compile` 校验，前端用 `node --check` 校验，行为用 ComfyUI 手动复现验证。

**参考 spec:** `docs/superpowers/specs/2026-09-06-r2v-continue-run-design.md`

---

## 文件结构

| 文件 | 责任 | 改动 |
|---|---|---|
| `director/plan.py` | 计划数据模型 + timeline→plan 解析 | 新增 `continue_run` 字段、`_resolve_continue_run`、报告行 |
| `director/gen_timeline.py` | r2v/gen 批量 plan 构建 | 传入 `continue_run` |
| `director/external_groups.py` | 外部接线组 plan 构建 | 传入 `continue_run` |
| `director/executor_core.py` | 采样/缓存回填/合并执行 | 放宽回填门控 + 续运行报告 |
| `web/js/minimax_timeline.js` | 时间轴编辑器主控 | 按钮 DOM/绑定、状态、toggles、payload、持久化、预览保留修复 |
| `web/js/minimax_i18n.js` | 中英文案 | 续运行相关键 |

---

## Task 1: 后端 `continue_run` 计划字段与解析

**Files:**
- Modify: `director/plan.py`（`DirectorPlan` 字段区 ~224-225；`_resolve_merge_only` ~588-599；merge_only 强制 export 区 ~796-800；DirectorPlan 构建 ~904-914；`_plan_report` merge_only 行 ~1026-1030）

- [ ] **Step 1: 加字段** — 在 `DirectorPlan` 的 `merge_only: bool = False` 之后新增：
```python
    continue_run: bool = False  # 「续运行」: 只采样勾选段，但按时间轴顺序合并全部段（缓存回填未勾选段）
```

- [ ] **Step 2: 加解析函数** — 在 `_resolve_merge_only` 函数之后新增（镜像其实现）：
```python
def _resolve_continue_run(timeline: dict) -> bool:
    """「续运行」: run only checked groups, but merge the FULL timeline in order.

    Implies run-select on the frontend (runSelectEnabled=true), so run_indices
    already carries the checked subset. Mutually exclusive with「仅合并缓存」.
    """
    val = timeline.get("continueRun")
    if val is None:
        val = timeline.get("continue_run")
    if val is None:
        return False
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "on", "yes"}
    return bool(val)
```

- [ ] **Step 3: 构建处写入** — 在 `director/plan.py` 的 `DirectorPlan(...)` 构建（视频/源时间轴分支，~904-914）中，`merge_only=merge_only` 一行后新增 `continue_run=_resolve_continue_run(timeline),`。同时在 `merge_only = _resolve_merge_only(timeline)`（~796）附近读取一次 `continue_run = _resolve_continue_run(timeline)` 以便报告/构建复用（避免重复解析）。

- [ ] **Step 4: 报告行** — 在 `_plan_report` 的 `if plan.merge_only:` 块（~1026-1030）之后新增：
```python
        if plan.continue_run:
            lines.append(
                "续运行「续运行」: ON — 只采样勾选段，按时间轴顺序合并全部段"
                "（未勾选段用缓存回填；缺缓存段跳过并在报告中列出）。"
            )
```

- [ ] **Step 5: 校验** — Run: `python -m py_compile director/plan.py` → Expected: 无输出（成功）

- [ ] **Step 6: 提交** — `git add director/plan.py && git commit -m "feat(director): add continue_run plan field + resolver"`

---

## Task 2: gen_timeline / external_groups 传入 continue_run

**Files:**
- Modify: `director/gen_timeline.py:672-695`（DirectorPlan 构建）
- Modify: `director/external_groups.py:620-642`（DirectorPlan 构建）

- [ ] **Step 1: gen_timeline** — 在 `gen_timeline.py` 的 `DirectorPlan(...)` 里 `merge_only=merge_only,`（~690）后新增：
```python
        continue_run=_resolve_continue_run(timeline),
```
并在文件顶部 import 区（已 `from .plan import (... _parse_run_selection,)` ~360）加入 `_resolve_continue_run`。

- [ ] **Step 2: external_groups** — 在 `external_groups.py` 的 `DirectorPlan(...)`（~620-642）里 `run_indices=run_indices,` 后新增：
```python
        continue_run=_resolve_continue_run(timeline),
```
并确认/新增 import：`from .plan import _resolve_continue_run`（该文件已 `from .plan import _parse_run_selection`，在同一 import 处补上）。

- [ ] **Step 3: 校验** — Run: `python -m py_compile director/gen_timeline.py director/external_groups.py` → Expected: 成功

- [ ] **Step 4: 提交** — `git add director/gen_timeline.py director/external_groups.py && git commit -m "feat(director): wire continue_run into gen/external-group plans"`

---

## Task 3: 续运行强制全量合并 + 报告

**实现调整（更稳健）**：不再逐个放宽 executor 的 `export_mode` 门控，而是在 plan 构建处
**当 `continue_run` 为真时强制 `export_mode="all"`**（镜像 `merge_only` 强制 `"segments"`），
直接复用久经考验的「全部导出」缓存回填+合并路径。executor 只新增续运行报告。

**Files:**
- Modify: `director/plan.py`（merge_only 强制 export 区 ~797）、`director/gen_timeline.py`（~451）、`director/external_groups.py`（DirectorPlan 前）
- Modify: `director/executor_core.py:1946-1951`（skipped 报告后追加续运行报告）

- [x] **Step 1: 强制 export=all** — 三处 plan 构建：`if merge_only: export_mode="segments"` 后加 `elif continue_run: export_mode="all"`；external_groups 在 DirectorPlan 前 `if continue_run: export_mode="all"`。

- [x] **Step 2: 续运行报告** — 在现有 `if skipped_no_cache:` 块之后新增：
```python
    if getattr(plan, "continue_run", False):
        sampled = sorted(int(i) + 1 for i in run_indices)
        merged_order = sorted(int(s.index) + 1 for s in output_segments)
        filled = [i for i in merged_order if i not in sampled]
        reports.append(
            f"续运行：本次采样 #{sampled}；缓存并入 #{filled}；"
            f"合并顺序 #{merged_order} → merged.mp4"
        )
        if skipped_no_cache:
            reports.append(
                f"续运行：#{skipped_no_cache} 无可用缓存，未并入 merged.mp4"
                "（请先运行该段，或将其一并纳入本次续运行）。"
            )
```

- [x] **Step 3: 校验** — `python3 -m py_compile director/{plan,gen_timeline,external_groups,executor_core}.py` → 成功

- [ ] **Step 4: 提交（待用户批准）**

---

## Task 4: 前端 continueRun 状态、toggles、payload、持久化

**Files:**
- Modify: `web/js/minimax_timeline.js`（`sanitizeBatchWorkspace` 341-353；ws 序列化 348-373；反序列化 1865-1972；`_captureBatchWorkspace` 4401-4419；`_persistCurrentBatchWorkspace` 4485-4508；`_applyBatchWorkspace` 4422-4444；`toggleRunSelectMode` 4029-4049；`toggleMergeOnlyMode` 4051-4062；`_clearLiveRunSelection` 4139-4143；`_runSelectionPayload` 4145-4161）

- [ ] **Step 1: sanitizeBatchWorkspace** — 在 `mergeOnly: !!ws.mergeOnly,`（~350）后加 `continueRun: !!ws.continueRun,`。

- [ ] **Step 2: ws 序列化/反序列化** — 在两处 `mergeOnly: !!ws.mergeOnly,`（~350/373 附近的 workspace 快照读写）后补 `continueRun`；在反序列化区（~1969-1972）`data.mergeOnly = ...` 后加：
```javascript
        data.continueRun = data.continueRun === true || data.continue_run === true;
        if (data.mergeOnly) { data.continueRun = false; }
```
并在默认态（~1867 `mergeOnly: false,` 附近）加 `continueRun: false,`。

- [ ] **Step 3: _captureBatchWorkspace / _persistCurrentBatchWorkspace / _applyBatchWorkspace** — 三处对象里 `runSelection`/`runSelectEnabled` 旁补 `continueRun: !!this.timeline.continueRun`（capture/persist）与 `this.timeline.continueRun = !!ws.continueRun`（apply）。

- [ ] **Step 4: toggleContinueRunMode（新增）** — 在 `toggleMergeOnlyMode()` 之后新增：
```javascript
    toggleContinueRunMode() {
        if (!this.supportsRunSelect()) return;
        this.timeline.continueRun = !this.timeline.continueRun;
        if (this.timeline.continueRun) {
            this.timeline.runSelectEnabled = true;
            this.timeline.mergeOnly = false;
            if (!(this.timeline.runSelection || []).length) {
                if (this.isFl2vMode()) {
                    this.timeline.runSelection = fl2vStartIndices(this);
                } else {
                    const n = this.getRunnableSegmentCount();
                    this.timeline.runSelection = Array.from({ length: n }, (_, i) => i);
                }
            } else {
                this.normalizeRunSelection();
            }
        } else {
            this.timeline.runSelectEnabled = false;
            this.timeline.runSelection = [];
        }
        this.updateRunSelectUI();
        this.commit(false, { syncTimeline: true });
        if (this.isImageBatch()) this.renderImageBatchGroups();
        else this.scheduleRender();
    }
```

- [ ] **Step 5: 互斥** — `toggleRunSelectMode()` 开启分支（`if (this.timeline.runSelectEnabled) {` 内、`this.timeline.mergeOnly = false;` 旁）加 `this.timeline.continueRun = false;`；`toggleMergeOnlyMode()` 开启分支加 `this.timeline.continueRun = false;`；`_clearLiveRunSelection()` 加 `this.timeline.continueRun = false;`。

- [ ] **Step 6: _runSelectionPayload** — 改为携带 continueRun：
```javascript
    _runSelectionPayload() {
        const canRunSelect = this.supportsRunSelect();
        const mergeOnly = canRunSelect && !!this.timeline.mergeOnly;
        if (mergeOnly) {
            return { runSelectEnabled: false, runSelection: [], mergeOnly: true, continueRun: false };
        }
        const continueRun = canRunSelect && !!this.timeline.continueRun;
        if (!canRunSelect || !this.timeline.runSelectEnabled) {
            return { runSelectEnabled: false, runSelection: [], mergeOnly: false, continueRun: false };
        }
        this.normalizeRunSelection();
        return {
            runSelectEnabled: true,
            runSelection: [...(this.timeline.runSelection || [])],
            mergeOnly: false,
            continueRun,
        };
    }
```

- [ ] **Step 7: 校验** — Run: `node --check web/js/minimax_timeline.js` → Expected: 无输出（成功）

- [ ] **Step 8: 提交** — `git add web/js/minimax_timeline.js && git commit -m "feat(web): continueRun state, toggles, payload + persistence"`

---

## Task 5: 前端按钮 DOM + 绑定 + updateRunSelectUI

**Files:**
- Modify: `web/js/minimax_timeline.js`（toolbar `buildDOM` 2722-2723；绑定 3203/3243；`updateRunSelectUI` 4081-4136）

- [ ] **Step 1: 按钮 DOM** — 在 `merge-only-toggle` 按钮（~2723）之后插入：
```html
                    <button type="button" class="bd-btn" data-a="run-continue-toggle" data-i18n="toolbar.continueRun" data-i18n-title="tooltip.continueRun">续运行</button>
```

- [ ] **Step 2: 绑定** — 在 `this.btnRunSelectToggle = ...`（~3203）旁加：
```javascript
        this.btnRunContinueToggle = this.root.querySelector('[data-a="run-continue-toggle"]');
```
在 `bind('[data-a="run-select-toggle"]', ...)`（~3243）旁加：
```javascript
        bind('[data-a="run-continue-toggle"]', () => this.toggleContinueRunMode());
```

- [ ] **Step 3: updateRunSelectUI** — 在计算 `enabled` 后加 `const continueRun = canRunSelect && !!this.timeline.continueRun && !mergeOnly;`；把 `this.btnRunSelectToggle?.classList.toggle("active", enabled);` 改为 `toggle("active", enabled && !continueRun)`；在 `btnMergeOnlyToggle` 显隐行之后加：
```javascript
        this.btnRunContinueToggle?.classList.toggle("active", continueRun);
        this.btnRunContinueToggle?.classList.toggle("hidden", !canRunSelect || useBatchBar);
```

- [ ] **Step 4: 摘要文案** — 在 `updateRunSelectUI` 摘要分支（~4126-4134）里，当 `continueRun` 为真时把 `exportHint` 替换为 `t("runSelect.continueHint")`（下一 Task 定义），其余逻辑不变。

- [ ] **Step 5: 校验** — Run: `node --check web/js/minimax_timeline.js` → Expected: 成功

- [ ] **Step 6: 提交** — `git add web/js/minimax_timeline.js && git commit -m "feat(web): 续运行 toolbar button + active/summary wiring"`

---

## Task 6: 预览保留修复（治症状 B）

**Files:**
- Modify: `web/js/minimax_timeline.js:12680-12689`（`executing` 事件回调）

- [ ] **Step 1: 只清将被重采段的预览** — 把
```javascript
            if (editor.isImageBatch?.()) {
                for (const seg of editor.timeline.segments || []) {
                    seg.previewB64 = "";
                    seg.previewFrames = [];
                    seg.previewLive = false;
                    seg.previewStep = null;
                    seg.previewTotalSteps = null;
                }
                editor.renderImageBatchGroups?.();
            }
```
改为
```javascript
            if (editor.isImageBatch?.()) {
                // 选择运行/续运行：只清「将被重采」段的预览，保留未勾选段的旧预览，
                // 否则缓存回填的段运行后渲染为空白，用户误以为原片段被清掉。
                const sel = editor.isRunSelectEnabled?.()
                    ? new Set(editor.timeline?.runSelection || [])
                    : null;
                (editor.timeline.segments || []).forEach((seg, i) => {
                    if (sel && !sel.has(i)) return;
                    seg.previewB64 = "";
                    seg.previewFrames = [];
                    seg.previewLive = false;
                    seg.previewStep = null;
                    seg.previewTotalSteps = null;
                });
                editor.renderImageBatchGroups?.();
            }
```

- [ ] **Step 2: 校验** — Run: `node --check web/js/minimax_timeline.js` → Expected: 成功

- [ ] **Step 3: 提交** — `git add web/js/minimax_timeline.js && git commit -m "fix(web): keep unselected group previews on partial/continue run"`

---

## Task 7: i18n 文案

**Files:**
- Modify: `web/js/minimax_i18n.js`（zh 区 `toolbar.runSelect`/`tooltip.*` 附近；en 区对应处）

- [ ] **Step 1: 中文键** — 在 zh 的 `"toolbar.runSelect": "选择运行",` 附近新增：
```javascript
    "toolbar.continueRun": "续运行",
    "tooltip.continueRun": "只采样勾选组，但按时间轴顺序把全部组合并为单一 merged.mp4（未勾选组用缓存回填；缺缓存的组会跳过并在报告中列出）。与「仅合并缓存」互斥。",
    "runSelect.continueHint": "· 续运行：未勾选组用缓存并入合并",
```

- [ ] **Step 2: 英文键** — 在 en 的 `"toolbar.runSelect": ...` 附近新增：
```javascript
    "toolbar.continueRun": "Continue-run",
    "tooltip.continueRun": "Sample only the checked groups, but merge ALL groups in timeline order into a single merged.mp4 (unchecked groups filled from cache; groups with no cache are skipped and listed in the report). Mutually exclusive with Merge-only.",
    "runSelect.continueHint": "· Continue-run: unchecked groups merged from cache",
```

- [ ] **Step 3: 校验** — Run: `node --check web/js/minimax_i18n.js` → Expected: 成功

- [ ] **Step 4: 提交** — `git add web/js/minimax_i18n.js && git commit -m "feat(web): i18n for 续运行 button + hint"`

---

## Task 8: 手动验证（ComfyUI 内，对照 spec 测试计划）

**Files:** 无（运行时验证）

- [ ] **Step 1: 续生成新段** — r2v，3 组，全部导出。先跑 #1、#2 → 开「续运行」→ 只勾 #3 → 运行。断言：运行后 #1/#2/#3 卡片预览都在（#1/#2 为旧预览）；`merged.mp4` = 1,2,3；报告含「续运行：本次采样 #[3]；缓存并入 #[1, 2]」。
- [ ] **Step 2: 中间段重跑** — 在 1,2,3 上续运行只勾 #2 → 运行。断言：`merged.mp4` = 1,2,3（#2 新采，#1/#3 缓存回填）。
- [ ] **Step 3: 缺缓存** — 删 `output/minimax_seg_cache/<node>/seg_0000.*` → 续运行只勾 #3 → 运行。断言：`merged.mp4` 跳过 #1；报告含「续运行：#[1] 无可用缓存，未并入 merged.mp4」；不自动补采。
- [ ] **Step 4: 持久化** — 开续运行 + 勾选后刷新页面/切任务再切回。断言：`continueRun`/`runSelectEnabled`/`runSelection`/各组内容与预览保留。
- [ ] **Step 5: 互斥** — 续运行开启时点「仅合并缓存」/「选择运行」。断言：状态按不变量切换（continueRun 与 mergeOnly 不同时为真；选择运行高亮在续运行时熄灭）。

---

## Self-Review

- **Spec 覆盖**：按钮（T5）、状态/toggles/payload/持久化（T4）、后端 flag+resolver（T1）、plan 构建接线（T2）、合并门控+报告（T3）、预览保留修复（T6）、i18n（T7）、测试计划（T8）——spec 各节均有对应任务。
- **占位扫描**：无 TBD/TODO；每步含具体代码或命令。
- **类型/命名一致**：前端状态键统一 `continueRun`（payload 同时容错 `continue_run`）；后端字段 `continue_run`、解析 `_resolve_continue_run`、按钮 `data-a="run-continue-toggle"` / `btnRunContinueToggle` / `toggleContinueRunMode()` 全程一致。
