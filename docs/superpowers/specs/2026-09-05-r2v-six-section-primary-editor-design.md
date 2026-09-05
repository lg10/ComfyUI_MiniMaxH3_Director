# r2v 六段式编辑器收敛为主编辑器 — 设计文档

日期：2026-09-05
分支：`feat/r2v-six-section-ui`

## 背景与问题

r2v 模式下用户报告两个缺陷：

1. **六段式编辑器与普通提示词编辑器同时存在。**
   根因不是设计意图，而是隐藏对象写错了。r2v 分支只执行 `textarea.style.display = "none"`
   （`minimax_timeline.js` `_updateGlobalR2vSections`、`minimax_image_batch.js` `appendBatchCard`），
   但普通提示词框在建 UI 时已被 `mountPromptImageMentions` → `ensureTokenShell` 改造：
   原 `<textarea>` 被塞进 `.bd-token-wrap` 成为 1px 隐形的 `.bd-token-source`，
   **真正可见的是同级新建的 contenteditable `.bd-token-editor`**（r2v 下 CSS 给它 `min-height:360px`）。
   因此 `display:none` 隐藏的是一个本来就不可见的元素，chip 编辑器照常撑开显示。

2. **六段式编辑器内无法 @ 出素材，也没有 chip 高亮样式。**
   @ 菜单与 chip 渲染是 `wirePromptImageMentions` 独占的能力（它把 textarea 换成 contenteditable
   token shell 才能承载原子 chip）。而六段式每一段是 `createR2vSectionsEditor` 里裸建的
   `<textarea class="r2v-section-textarea">`，该模块只 import 了 `t`，从未调用
   `wirePromptImageMentions`。

## 决策记录

| 决策点 | 选择 | 理由 |
|---|---|---|
| 两个编辑器的关系 | 六段式为唯一主编辑器，普通 chip 编辑器移入默认折叠容器 | 界面干净，同时保留直接编辑原文的逃生口 |
| 折叠视图是否可编辑 | 可编辑，**完全不回灌**六段式 | 避免 `parseR2vSections` 反解析失败时把整段文本误塞进 `subject_definitions` |
| 折叠视图范围 | 各管一段：公共列 = `global.prompt`（前三段），分组卡片 = `seg.prompt`（后三段） | 可编辑视图必须与单一权威字段 1:1，否则写回归属不明 |
| 最终发送串预览 | 分组卡片额外提供只读预览 | 用户需要核对真正下发给模型的内容 |

## 关键事实：真正下发给模型的提示词

两步变换，均为纯函数、前端可完整镜像：

1. **拼接**（`director/plan.py` `concat_common_segment_prompt`，由 `gen_timeline.py` /
   `external_groups.py` 调用）：r2v + `commonEnabled` 时为 `global.prompt.strip()` + 空行 +
   `seg.prompt.strip()`；仅一侧非空时取该侧。
2. **补标签**（`director/executor_core.py` 第 1121-1146 行，对**所有** r2v 段执行）：
   `positive_prompt = reinforce_r2v_prompt(seg.prompt, ...)`，其中 `seg.prompt` 已是第 1 步结果。
   规则：trim 后为空 → `"Generate a cinematic scene."`；文本不含 `<Picture` 且存在图片索引 →
   前置 `<Picture i+1>…`；`<Video` / `<Audio` 同理。

## 实现设计

### 1. 真正隐藏普通编辑器

隐藏对象从 `<textarea>` 改为其可见外壳 `.bd-token-wrap`（`promptEl.__bdTokenWrap` /
`this.globalPrompt.__bdTokenWrap`）。不删除，而是移进第 3 节的折叠容器。
非 r2v 模式恢复显示。

### 2. 六段式各段接入 @ 与 chip

`createR2vSectionsEditor` 新增可选项 `editorHost` 与 `getMedia`；两者齐备时对每个段 textarea 调
`wirePromptImageMentions(..., { compact: true })`。分组卡片复用现有 `mergeMediaByIndex(公共, 本组)`
合并逻辑，公共列传全局媒体。

必须处理的三个坑：

- **CSS 特异性**：多条现有规则会把段内编辑器撑成完整提示词框的尺寸，其中最狠的是
  `.bd-wrap.bd-batch-fill .bd-batch-list.bd-batch-solo .bd-token-editor`（特异性 (0,5,0)）与
  `.bd-batch-r2v .bd-batch-prompts textarea,.bd-token-wrap{min-height:360px}`。靠“另写一条更高特异性
  规则”会陷入军备竞赛，而且 `MENTION_STYLES` 与 `SECTION_STYLES` 分属两个模块各自懒注入，
  **注入先后不可靠**，同特异性时胜负随机。
  解决：给段内外壳打 `.bd-token-compact` 标记，把所有“放大提示词框”的竞争规则改写为
  `:not(.bd-token-compact)`（wrap）与 `:not(.bd-token-source)`（隐形 textarea），
  段内尺寸由 `.bd-token-wrap.bd-token-compact>.bd-token-editor{min-height:80px;font-size:11px}` 单独定义。
- **拖拽把手**：`installTokenResizeHandle` 触发条件是 `closest(".bd-batch-r2v")`，段内编辑器会各长出
  一个把手（6 个）。`ensureTokenShell` 新增 `compact` 开关，紧凑外壳不装把手，保留 `overflow:auto` 滚动。
- **丢字竞态**：chip 编辑器 → textarea 有 ~80ms 延迟（`scheduleTagRehydrate`）。六段式 `flush()`
  在读 `textarea.value` 前必须先对每个段调 `__bdTokenApi.sync()`；`flushBatchPromptInputs` 同样在
  循环开头先 sync，否则 DOM 重建前会丢最后几个字。

### 3. 折叠式「完整提示词」可编辑视图

复用第 1 节移过来的 token 编辑器（已带 @ 与 chip，`oninput` 已写 `seg.prompt` / `global.prompt`
及外部 Group widget，行为不改）：

```
提示词
└ 六段式编辑器（主，@ 已接入）
└ [▸ 查看/编辑完整提示词]           ← 默认折叠
   ⚠ 此处修改直接写入本段提示词，不回灌六段式；六段式下次编辑会覆盖它
   <chip 编辑器>
```

**必须修掉的隐性反向同步**：`_updateGlobalR2vSections` 原本每次 `updateModeUI()` 都无条件调
`_globalR2vEditor.refresh()`，而 `refresh()` = 解析 `global.prompt` 并覆盖六段式。这会让完整视图里的
手改在下次切模式时被自动灌回六段式，违背「完全不回灌」。确立原则：**refresh 只在编辑器（重）建时
发生，存活期间永不自动 refresh**；离开 r2v 时 `destroy()` 编辑器。已有的「↓ 从提示词」按钮保留，
作为用户手动拉取的唯一入口。

**完整视图必须永久嵌套**：公共列的外壳一次性包进 `.bd-r2v-full`，之后只切 `collapsed` /
`is-plain` 两个类，**永不把 token wrap 移回原位**。移入移出会丢监听器并需要写 DOM 归位逻辑。
`_ensureGlobalR2vFullView()` 带闩锁：`updateModeUI()` 可能早于 `mountPromptImageMentions()` 运行，
此时外壳尚不存在；`_globalR2vSectionsActive` 只在外壳真的建好后才置位，否则首次跳变会被吞掉、
普通编辑器永久展开。`mountPromptImageMentions(this)` 之后立即补调一次
`_updateGlobalR2vSections(...)`，彻底消除这个顺序依赖。

**六段式写回必须同步 DOM textarea**：`onGlobalField("prompt", v)` 只写 `timeline.global.prompt` 与
`globalPromptWidget.value`，**不写** `this.globalPrompt.value`。折叠视图因此会显示陈旧内容，
需在六段式 `onSetPrompt` 里显式补写（经覆写的 setter 触发 `hydrateFromValue` 重建 chip）。

**卡片重建不得丢弃完整视图的手改**：改秒数/增删组会整体重建卡片。若照旧先无条件 flush 六段式
（用陈旧 sections 覆盖 `seg.prompt`）、再因 `batchR2vHidden` 跳过 raw textarea，用户的手改会静默消失。
引入 `promptEl.dataset.batchR2vRawEdited` 仲裁：完整视图 `input` 时置 `"1"`，六段式 `onSetPrompt` 时
`delete`；flush 时若为真则跳过六段式 flush，让 raw 文本作为权威写回 `seg.prompt`。

**未动过的六段式不得写回**：`parseR2vSections` 只认得它自己的格式，对自由文本返回 `null`。
旧逻辑下，卡片重建时无条件 `flush()` 会把空 sections 组装成空串并写回 `seg.prompt`，
**直接抹掉用户原有的提示词**（此隐患早于本次改动，但六段式成为主编辑器后命中率大幅上升）。
因此给编辑器加 `dirty` 标志：仅当用户真的改过段内容、或显式插入模板/导入剧本/`setSections` 时置位，
`flush()` 与 `destroy()` 在 `dirty` 为假时是 no-op；`refresh()`（= `syncFromPrompt`）拉入权威文本
**不算编辑**，不置位。同时 `destroy()` 改为先 flush 再 teardown（chip 的 `sync()` 需要活 DOM），
修掉“切模式时 300ms 内的最后几个字被吞”。

### 4. 只读「最终发送串」预览（仅分组卡片）

前端镜像上述两步变换，落在新模块 `web/js/minimax_r2v_final_prompt.js`：三个纯函数、零依赖，
因此可直接在 node 下与 Python 原版对拍（见「验证」）。`minimax_r2v_sections.js` 只 re-export，
避免出现第二份实现。

索引取自与 @ 菜单相同的合并后媒体列表（`commonEnabled` 时 `merge_indexed_refs(公共, 本组)`，
否则仅本组），保证「@ 能选到的」与「预览里出现的」是同一批 slot。只读 `<pre>` + 「复制」按钮
（复制走 `navigator.clipboard`，失败时回退 `execCommand`，因为 canvas widget 可能跑在纯 http 下）。
刷新时机：展开时，以及六段式/完整视图任一方写入后。
公共列不放此预览（它只是前缀，无独立“最终串”），改用 `r2v.full.warnCommon` 文案。

### 5. i18n

新增 zh/en key：`r2v.full.toggle`、`r2v.full.warn`、`r2v.full.warnCommon`、`r2v.final.title`、
`r2v.final.copy`、`r2v.final.copied`。分组卡片走 `innerHTML` 重建，直接用 `t()`；公共列静态节点补
`data-i18n` 以便 `applyI18nDom` 热切换。

## 边界

- 不动后端 Python
- 不动非 r2v 模式的任何行为（同一个 `.bd-r2v-full` 外壳以 `is-plain` 常驻展开，视觉与改动前一致）
- `promptEl.oninput` / `globalPrompt.oninput` 原有写回逻辑一行不改，只额外挂一个监听器打
  `batchR2vRawEdited` 标记
- `.bd-batch-plain` / `.bd-batch-source` 下的 token 规则**故意不加** `:not()`：卡片 class 由
  `card.className` 一次性赋为 `bd-batch-card` + 单一 `layoutClass`，与 `.bd-batch-r2v` 互斥，
  六段式不可能出现在那里
- 六段式内部 `sections` 状态在完整视图被编辑后会与 `seg.prompt` 不一致，这是「不回灌」的必然结果；
  不做额外“已过期”标记（YAGNI）

## 涉及文件

- `web/js/minimax_r2v_final_prompt.js`（**新增**，零依赖纯函数镜像）
- `web/js/minimax_r2v_sections.js`（接入 @、折叠视图组件、re-export 镜像）
- `web/js/minimax_timeline.js`（公共列折叠 + 生命周期闩锁 + CSS 收窄）
- `web/js/minimax_image_batch.js`（分组卡片折叠 + rawEdited 仲裁 + CSS 收窄）
- `web/js/minimax_prompt_mentions.js`（`compact` 开关、把手抑制、CSS 收窄）
- `web/js/minimax_i18n.js`（6 个 key × zh/en）

## 验证

- `node --check` 全部 6 个文件（`.js` 被 node 当 CJS 解析，需复制为 `.mjs` 再校验）
- **JS ↔ Python 对拍**：用 `ast` 从 `director/plan.py` 抽出 `concat_common_segment_prompt` 与
  `reinforce_r2v_prompt`（两者是纯函数，无需 torch），对 18 个用例逐一比对
  `final` / `concat` / `reinforce` 三个断言，全部一致。用例覆盖：两侧均非空、仅一侧非空、
  全空回退 `"Generate a cinematic scene."`、`commonEnabled=false` 走 `local_prompt or prompt`、
  文本已含 `<Picture` / `<picture>` 时不前置、三族标签部分提及、索引去重升序、负索引丢弃、
  `slot` 键与裸数字、畸形条目忽略、空白 prompt + 有媒体、中文与换行原样保留。
- 手工验证清单：r2v 公共列只显示六段式；六段式各段可 @ 出素材并渲染 chip；
  折叠视图默认收起、展开后可编辑且带警示条；最终串预览与后端 `reinforce(concat(...))` 一致；
  切到 t2v/i2v 后普通编辑器恢复；分组卡片重建（改秒数/增删组）不丢字，且完整视图的手改不被六段式覆盖。
- 已验证（本次）：6 个文件 `node --check` 全通过；18 个用例 × 3 断言与 Python 全部一致。
- 待人工实机验证（需跑 ComfyUI）：上述手工清单全部项目。
