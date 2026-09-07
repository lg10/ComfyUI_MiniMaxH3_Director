# r2v「续运行」按钮 — 设计文档

日期：2026-09-06
范围：r2v（`prompt_batch` 模式，走图片/视频批量面板）；导出模式 = 全部导出（`export_mode="all"`）

## 背景与问题

用户在 r2v 模式下做「续生成 / 中间片段生成」时报告两个缺陷：

- **症状 A：`merged.mp4` 只含本次新跑的段**，之前已生成的段没有并入。
- **症状 B：时间轴上原片段的卡片「被清掉 / 看不到」**，用户不确定缓存是否还在。

期望行为（用户原话）：曾经生成过 1、2 段后，「续运行 3」→ 最终合并 `1,2,3`；「续生成 2」→ 最终合并 `1,2`。即**合并结果永远是时间轴上所有段的有序拼接**（本次采样的段 + 之前缓存的段），不因只跑部分段而丢失前面的成果。

### 现状（已核验的代码事实）

1. r2v → `getDirectorMode` 返回 `"prompt_batch"` → `isImageBatch()` 为真，走批量面板
   （`web/js/minimax_image_batch.js`），工具栏已有「选择运行」按钮 `bd-batch-run-select`。
2. 批量 payload 分支（`minimax_timeline.js` `buildPayload` 的 `isImageBatch()` 段，约 2512–2572 行）
   **总是把 `timeline.segments` 全量下发**，另附 `_runSelectionPayload()` 给出的
   `runSelectEnabled` / `runSelection`（勾选的组序号）。
3. 后端 `gen_timeline.py` / `external_groups.py` **保留完整段列表**，用 `run_indices`
   标记本次采样哪些段（不压缩 plan）。
4. 后端合并（`executor_core.py` 约 1865–1900 行）：**仅当 `export_mode == "all"`** 时，
   未选中的段才从磁盘缓存回填并并入合并；**缓存未命中则该段被静默跳过**
   （`skipped_no_cache`），不并入 `merged.mp4`。
5. 磁盘缓存写入条件（`_segment_disk_cache_needed`）：段数 ≥ 2、或 refine、或 continuity 时写入；
   `prune_segment_cache` 按**全部当前段序号**保留（不按 `run_indices`），故未选中的段缓存本应保留。
6. 缓存命中受指纹门控（`_segment_identity_fingerprint`：index/start/end/prompt/refs +
   全局 width/height/frame_rate/output_mode/ref_max + continuity 相关键 + `prev_chain`）。
   `prev_chain` 为 safe-reuse 只影响状态提示、不否决命中。

### 根因判断（已定位）

- **症状 B 的精确根因**：`minimax_timeline.js` 的 `executing` 事件回调（约 12680–12689 行）在
  **每次运行开始时清空全部批量段的预览**（`previewB64=""`、`previewFrames=[]`）。选择运行/续运行时，
  只有**被采样的段**会通过 `minimax_director_preview` 事件重新拿到预览；**缓存回填的未勾选段不会收到任何预览事件**，
  于是运行后它们渲染为空白 → 用户以为「原片段被清掉」。
  实际上 `timeline.segments` 与磁盘缓存都还在，payload 也仍全量下发——**这是「看不到」而非「真被删」**。
- **症状 A** 有两种可能：
  1. 主要是 B 造成的错觉——卡片空白让用户以为合并丢了前面的段（若缓存命中，`merged.mp4` 其实已是 1,2,3）。
  2. 若前置段指纹漂移导致缓存未命中，`export=all` 分支会静默跳过该段（`skipped_no_cache`），合并确实变短；
     当前报告对此提示很弱，用户难以察觉。
- 因此修复分三处：**(1) 运行开始只清「将被重采」段的预览，保留未勾选段预览**（治 B）；
  **(2) 续运行保证全时间轴有序合并**（治 A-1/A-2 的合并完整性）；**(3) 缺缓存显著报告**（治 A-2 的可观测性）。

## 决策记录

| 决策点 | 选择 | 理由 |
|---|---|---|
| 交互形态 | **新增独立「续运行」按钮**，放在批量工具栏「选择运行」旁边 | 用户明确要求；语义比复用「选择运行」更清晰 |
| 「续运行」合并语义 | 只采样勾选段，但**永远按时间轴顺序合并全部段**（勾选=新采，未勾选=缓存回填）→ 单一 `merged.mp4` | 直接对应「1,2,3」期望 |
| 与「选择运行」关系 | 互斥的两种运行姿态，但**共用同一套勾选框 UI 与 `runSelection`**；「续运行」在内部令 `runSelectEnabled=true` 以复用现有勾选逻辑，另加 `continueRun` 标志改变合并行为 | 避免重复实现勾选/进度逻辑 |
| 与「仅合并缓存」关系 | 互斥（开一个自动关另一个） | 三者语义互斥，避免状态冲突 |
| 未勾选前置段**缺缓存**时 | **只报告、不自动补采**（用户决策）；缺缓存段跳过、不并入 `merged.mp4`，报告显著列出 | 用户明确选择；避免意外多采样、成本不可控 |
| 适用模式范围 | 仅 r2v / 图片-视频批量（`prompt_batch`）面板显示按钮；状态与 payload 字段通用化但不破坏其他模式 | YAGNI，聚焦用户实际场景 |

## 状态模型

新增一个布尔状态 `continueRun`，与既有 `runSelectEnabled`、`mergeOnly` 平级，存放于
`timeline` 及批量工作区快照中。不变量：

- `continueRun === true` ⟹ `runSelectEnabled === true` 且 `mergeOnly === false`。
- 「选择运行」active 显示 = `runSelectEnabled && !continueRun`；「续运行」active 显示 = `continueRun`。
- 三者互斥关系由 toggle 方法维护（见下）。

## 前端改动（web/js）

> **按钮位置更正**：r2v 下 `isR2vBatch()` 为真 → `useBatchBar` 为假 → 运行选择控件走
> **时间轴工具栏**（`btnRunSelectToggle` / `btnMergeOnlyToggle`，fl2v 风格时间轴勾选框），
> 而非批量工具栏。故「续运行」按钮加在**时间轴工具栏**，显隐门控与 `btnRunSelectToggle` 完全一致。

### 1. `minimax_image_batch.js`

- `wireBatchRunSelectControls`：无需新增按钮（r2v 不用批量条）；保持现状。
  （非 r2v 批量任务 t2v/i2v 不在本次范围，续运行按钮不加入批量条。）

### 2. `minimax_timeline.js`

- `buildDOM` 工具栏（约 2722–2723 行）：在 `merge-only-toggle` 之后新增
  `<button type="button" class="bd-btn" data-a="run-continue-toggle" data-i18n="toolbar.continueRun" data-i18n-title="tooltip.continueRun">续运行</button>`。
- 绑定处（约 3203/3243 行）：`this.btnRunContinueToggle = this.root.querySelector('[data-a="run-continue-toggle"]')`；
  `bind('[data-a="run-continue-toggle"]', () => this.toggleContinueRunMode())`。

- 新增 `toggleContinueRunMode()`：
  - 关→开：`continueRun=true`；`runSelectEnabled=true`；`mergeOnly=false`；
    若 `runSelection` 为空则按现有 `toggleRunSelectMode` 的方式初始化为全部组序号
    （`Array.from({length:n},(_,i)=>i)`，fl2v 分支用 `fl2vStartIndices`）。
  - 开→关：`continueRun=false`；`runSelectEnabled=false`；`runSelection=[]`。
  - 结尾调用 `updateRunSelectUI()` + `commit(false,{syncTimeline:true})` + 重渲染（与 `toggleRunSelectMode` 一致）。
- 改 `toggleRunSelectMode()`：开启「选择运行」时置 `continueRun=false`。
- 改 `toggleMergeOnlyMode()`：开启「仅合并缓存」时置 `continueRun=false`。
- 改 `updateRunSelectUI()`：
  - `btnRunContinueToggle` 的显隐门控与 `btnRunSelectToggle` 一致（`hidden = !canRunSelect || useBatchBar`）；
    active 态 = `continueRun && canRunSelect && !mergeOnly`。
  - `btnRunSelectToggle` 的 active 改为 `enabled && !continueRun`（续运行时「选择运行」不高亮）。
  - 勾选框可见性继续以 `runSelectEnabled` 为准（`continueRun` 已蕴含它为真，无需改勾选框渲染逻辑）；
    摘要文案在 `continueRun` 时用续运行专用 i18n。
- 改 `_runSelectionPayload()`：
  - `mergeOnly` 分支不变（返回 `continueRun:false`）。
  - `continueRun` 为真时返回 `{ runSelectEnabled:true, runSelection:[...], mergeOnly:false, continueRun:true }`。
  - 其余情况在返回对象中补 `continueRun:false`（含「不支持/未开启」的早退分支）。
- **工作区持久化**：在 `_captureBatchWorkspace`、`_persistCurrentBatchWorkspace`、`_applyBatchWorkspace`、
  `sanitizeBatchWorkspace`（及 348–373、1865–1972 的 ws 序列化/反序列化）中增补 `continueRun` 字段，
  保证刷新/切换后状态与勾选不丢。
- **预览保留（治症状 B，核心修复）**：改 `executing` 事件回调（约 12680–12689 行）——
  当 `runSelectEnabled`（含 `continueRun`）为真时，**只清空 `runSelection` 内段的预览**，
  未勾选段的 `previewB64`/`previewFrames`/`previewLive`/`previewStep` 全部保留；
  未开启选择运行（全跑）时维持现状清空全部。这样续运行后 #1/#2 卡片仍显示旧预览，用户可见其未被清掉。
- **不变量加固**：`renderImageBatchGroups` 在非「单显模式」下始终渲染 `timeline.segments` 全量（现状已如此）；
  确认 `_switchToBatchTaskWorkspace` / `_stashBatchWorkspace` / `_restoreBatchWorkspace` 在续运行开关切换时
  不会换入更小的快照（`toggleContinueRunMode` 不触发任务切换，故不受影响）。

### 3. `minimax_i18n.js`

新增中英键：`toolbar.continueRun`（续运行 / Continue-run）、`tooltip.continueRun`
（说明：只跑勾选组，但按时间轴顺序合并全部组为单一 merged.mp4；未勾选组用缓存回填，缺缓存的组会跳过并在报告中列出）、
以及续运行摘要文案键（如 `runSelect.continueSummary`、`runSelect.continueMissingCache`）。

## 后端改动（director）

### 1. `plan.py`

- `DirectorPlan` 新增字段 `continue_run: bool = False`。
- 新增 `_resolve_continue_run(timeline)`，镜像 `_resolve_merge_only`（读 `continueRun` / `continue_run`，
  支持字符串 `"1"/"true"/"on"/"yes"`）。
- 在 r2v 相关的 plan 构建处（`gen_timeline.py` 的批量/gen 分支，以及 `external_groups.py`）
  写入 `continue_run=_resolve_continue_run(timeline)`。
- 当 `continue_run` 为真：保证产出**全时间轴单一 `merged.mp4`**（等同 `export=all` 的合并语义）。
  若用户处于 `export_mode="segments"`，仍照常写各段 mp4，但额外强制全量合并；
  `run_indices` 仍由 `_parse_run_selection` 得出（因为 payload 里 `runSelectEnabled=true`）。
- `_plan_report`：`continue_run` 为真时追加一行说明（镜像 `merge_only` 的报告行）。

### 2. `executor_core.py`

- 放宽缓存回填门控：把 `if plan.export_mode != "all": continue`（约 1865 行）改为
  `if plan.export_mode != "all" and not plan.continue_run: continue`，使续运行在任何导出模式下
  都对未勾选段执行「缓存回填 + 全量有序合并」。
- **缺缓存只报告不补采**：沿用现有 `skipped_no_cache` 收集；续运行时把报告做显著：
  - 汇总行示例：`续运行：本次采样 #3；缓存并入 #1,#2；合并顺序 #1,#2,#3 → merged.mp4`。
  - 缺缓存行示例：`续运行：#1 无可用缓存，未并入 merged.mp4（请先运行该段，或将其一并纳入本次续运行）`。
- 合并顺序、流式合并（`stream_merge_active`）、音频对齐等沿用既有逻辑，不新增采样。

## 报告 / 可观测性

- 控制台与报告输出明确区分：**本次采样段** / **缓存回填段** / **缺缓存跳过段**，
  并给出最终 `merged.mp4` 的段顺序。用户据此可判断是否需要补跑缺缓存段。

## 测试计划（手动，在 ComfyUI 内）

前置：r2v，3 个素材组，导出模式=全部导出。

1. **续生成新段**：先跑 #1、#2（生成缓存）→ 开启「续运行」→ 只勾 #3 → 运行。
   断言：运行后 #1/#2/#3 卡片均在；`merged.mp4` = 1,2,3；报告显示 #1,#2 为缓存回填。
2. **中间段重跑**：在 1,2,3 基础上，续运行只勾 #2 → 运行。
   断言：`merged.mp4` = 1,2,3，其中 #2 为新采样，#1/#3 缓存回填。
3. **缺缓存**：手动删除 #1 缓存 → 续运行只勾 #3 → 运行。
   断言：`merged.mp4` 跳过 #1（= 2,3），报告显著列出「#1 无可用缓存，未并入」，不自动补采。
4. **状态持久化**：开启续运行 + 勾选后刷新页面 / 切任务再切回。
   断言：`continueRun`、`runSelectEnabled`、`runSelection` 与各组内容/预览均保留。
5. **互斥**：续运行开启时点「仅合并缓存」/「选择运行」。
   断言：状态按不变量切换，无冲突（continueRun 与 mergeOnly 不同时为真）。

## 范围与非目标（YAGNI）

- 不在 video / fl2v 工具栏新增该按钮（用户场景为 r2v）；状态与 payload 字段保持通用、不破坏其他模式。
- 不改动缓存指纹算法；缺缓存一律「只报告不补采」。
- 不引入自动补采、自动清缓存等新行为。
