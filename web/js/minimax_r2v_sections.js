/** MiniMax H3 r2v six-section structured prompt editor.
 *
 * Provides a visual editor for the official six-section r2v prompt format:
 *   subject_definitions → summary → retention_analysis →
 *   detailed_description → overall_soundscape → non_diegetic_music
 *
 * Each section has its own textarea with status badge. Bidirectional sync
 * between the combined prompt text and individual section editors.
 *
 * Integration: mount below the prompt enhancer panel in r2v mode.
 */

import { t } from "./minimax_i18n.js";
import { teardownPromptImageMentions, wirePromptImageMentions } from "./minimax_prompt_mentions.js";

// ─── Constants ───────────────────────────────────────────────────────────────

/** Official six-section order (immutable per ref-en.txt §1). */
export const SECTION_NAMES = [
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
];

/** First three sections → Director common/global prompt (shared across groups). */
export const COMMON_SECTIONS = SECTION_NAMES.slice(0, 3);

/** Last three sections → Director segment/group prompt (per-shot). */
export const SEGMENT_SECTIONS = SECTION_NAMES.slice(3);

/** Human-readable labels for each section (i18n keys). */
const SECTION_LABELS = {
    subject_definitions: "r2v.section.subject_definitions",
    summary: "r2v.section.summary",
    retention_analysis: "r2v.section.retention_analysis",
    detailed_description: "r2v.section.detailed_description",
    overall_soundscape: "r2v.section.overall_soundscape",
    non_diegetic_music: "r2v.section.non_diegetic_music",
};

/** Short descriptions shown as placeholder hints. */
const SECTION_HINTS = {
    subject_definitions: "<Subject 1> A young woman with long black hair…",
    summary: "reference generation + audio reuse: <Subject 1> walks through…",
    retention_analysis: "<Subject 1> fully_preserved; <Audio 1> partially_copy…",
    detailed_description: "[Shot 1] The scene opens in a crowded urban street…",
    overall_soundscape: "City traffic hum, distant sirens, footsteps on pavement…",
    non_diegetic_music: "Soft piano melody with strings, slow tempo, gentle dynamics…",
};

/** Section header colors (match token chip colors). */
const SECTION_COLORS = {
    subject_definitions: "#a78bfa",  // purple (Subject)
    summary: "#4d9fff",              // blue
    retention_analysis: "#e8a23a",   // orange
    detailed_description: "#3dcc7a", // green
    overall_soundscape: "#f472b6",   // pink
    non_diegetic_music: "#fbbf24",   // yellow
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Count words in a string (whitespace-separated tokens). */
function countWords(str) {
    const content = String(str || "").trim();
    return content ? content.split(/\s+/).filter(Boolean).length : 0;
}

/** Read all textarea values into sections object (mutates sections).
 *  Iterates only over mounted editors so subset mode (common/segment) works.
 *  Chip editors lag their textarea by ~80ms (debounced tag rehydrate) — flush
 *  them first, otherwise a card rebuild drops the last few keystrokes. */
function readTextareasIntoSections(sectionEditors, sections) {
    for (const name of Object.keys(sectionEditors)) {
        const editor = sectionEditors[name];
        if (editor) {
            editor.textarea.__bdTokenApi?.sync?.();
            sections[name] = editor.textarea.value;
        }
    }
}

/** Copy text without relying on navigator.clipboard (canvas widgets may be
 *  served over plain http, where the async clipboard API is unavailable). */
function copyTextToClipboard(text) {
    const value = String(text ?? "");
    if (navigator.clipboard?.writeText) {
        return navigator.clipboard.writeText(value).then(() => true, () => legacyCopy(value));
    }
    return Promise.resolve(legacyCopy(value));
}

function legacyCopy(value) {
    const ta = document.createElement("textarea");
    ta.value = value;
    // Off-screen but focusable; execCommand needs a live selection.
    ta.setAttribute("readonly", "");
    Object.assign(ta.style, { position: "fixed", top: "-1000px", opacity: "0" });
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {
        ok = document.execCommand("copy");
    } catch {
        ok = false;
    }
    ta.remove();
    return ok;
}

// ─── Styles ──────────────────────────────────────────────────────────────────

const SECTION_STYLES = `
.r2v-sections-panel{
  display:flex;flex-direction:column;gap:8px;padding:8px;
  background:#1a1a1a;border:1px solid #333;border-radius:8px;
  max-height:600px;overflow-y:auto
}
.r2v-sections-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:4px 0;border-bottom:1px solid #2a2a2a;margin-bottom:4px
}
.r2v-sections-title{
  font-size:12px;font-weight:600;color:#4fff8f;
  display:flex;align-items:center;gap:6px
}
.r2v-sections-actions{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end}
.r2v-section-btn{
  padding:3px 8px;font-size:10px;border-radius:4px;cursor:pointer;
  border:1px solid #444;background:#252525;color:#ddd;
  transition:all .15s
}
.r2v-section-btn:hover{background:#333;border-color:#4fff8f;color:#4fff8f}
.r2v-section-btn.primary{background:#10b981;border-color:#10b981;color:#fff}
.r2v-section-btn.primary:hover{background:#059669}
.r2v-section-item{
  display:flex;flex-direction:column;gap:4px;
  padding:8px;background:#222;border-radius:6px;
  border-left:3px solid #444;transition:border-color .2s
}
.r2v-section-item:focus-within{border-left-color:var(--section-color,#4fff8f)}
.r2v-section-item-header{
  display:flex;align-items:center;justify-content:space-between;gap:8px
}
.r2v-section-label{
  font-size:11px;font-weight:600;color:#ccc;
  display:flex;align-items:center;gap:4px
}
.r2v-section-label .dot{
  width:8px;height:8px;border-radius:50%;
  background:var(--section-color,#4fff8f);flex-shrink:0
}
.r2v-section-badge{
  font-size:9px;padding:2px 6px;border-radius:10px;
  background:#333;color:#888;border:1px solid #444
}
.r2v-section-badge.filled{background:#1a3a2a;color:#4fff8f;border-color:#2a5a3a}
.r2v-section-badge.empty{background:#3a2a1a;color:#e8a23a;border-color:#5a3a2a}
.r2v-section-textarea{
  width:100%;min-height:80px;padding:8px;
  background:#181818;border:1px solid #333;border-radius:4px;
  color:#eee;font-size:11px;font-family:inherit;line-height:1.4;
  resize:vertical;outline:none;box-sizing:border-box
}
.r2v-section-textarea:focus{border-color:#4a7a5a;box-shadow:0 0 0 1px rgba(79,255,143,.18)}
.r2v-section-textarea::placeholder{color:#555}
.r2v-sections-footer{
  display:flex;align-items:center;justify-content:space-between;
  padding:6px 0;border-top:1px solid #2a2a2a;margin-top:4px;
  font-size:10px;color:#888
}
.r2v-sections-stats{display:flex;gap:12px}
.r2v-sections-stat{display:flex;align-items:center;gap:4px}
.r2v-sections-stat .value{color:#4fff8f;font-weight:600}
.r2v-sections-toggle{
  display:flex;align-items:center;gap:6px;padding:6px 10px;
  background:#252525;border:1px solid #333;border-radius:6px;
  cursor:pointer;font-size:11px;color:#ddd;transition:all .15s
}
.r2v-sections-toggle:hover{border-color:#4fff8f;color:#4fff8f}
.r2v-sections-toggle.active{background:#1a3a2a;border-color:#4fff8f;color:#4fff8f}
.r2v-sections-toggle .icon{font-size:14px}
.r2v-sections-collapsed .r2v-sections-panel{display:none}

/* Section rows host a compact chip editor once @-mentions are wired. */
.r2v-section-item>.bd-token-wrap.bd-token-compact{flex:0 0 auto;width:100%}

/* Collapsible raw-prompt view. r2v keeps the six-section editor as the only
   primary surface; the chip editor stays reachable but folded, and its edits
   never flow back into the sections. */
.bd-r2v-full{display:flex;flex-direction:column;gap:4px;min-width:0;width:100%}
.bd-r2v-full-toggle{
  display:flex;align-items:center;gap:6px;align-self:flex-start;
  padding:4px 10px;background:#252525;border:1px solid #333;border-radius:6px;
  cursor:pointer;font-size:11px;color:#ddd;transition:all .15s
}
.bd-r2v-full-toggle:hover{border-color:#4fff8f;color:#4fff8f}
.bd-r2v-full-toggle .caret{font-size:9px;color:#888;transition:transform .15s}
.bd-r2v-full:not(.collapsed) .bd-r2v-full-toggle{background:#1a3a2a;border-color:#4fff8f;color:#4fff8f}
.bd-r2v-full:not(.collapsed) .bd-r2v-full-toggle .caret{transform:rotate(90deg);color:#4fff8f}
.bd-r2v-full.collapsed .bd-r2v-full-body{display:none}
/* Non-r2v modes reuse the same shell as a plain, always-open prompt box: the
   toggle and the warning are r2v-only chrome. Keeping the wrap permanently
   nested here means it never has to be moved back. */
.bd-r2v-full.is-plain>.bd-r2v-full-toggle,
.bd-r2v-full.is-plain .bd-r2v-full-warn,
.bd-r2v-full.is-plain .bd-r2v-final{display:none}
/* Nesting the wrap here put two non-growing boxes between it and the column, so
   plain mode has to carry the grow the wrap used to take from .bd-prompt-col
   directly. Without this every non-r2v prompt box collapses to min-height. */
.bd-r2v-full.is-plain{flex:1 1 auto;min-height:96px}
.bd-r2v-full.is-plain>.bd-r2v-full-body{display:flex;flex:1 1 auto;min-height:0}
.bd-r2v-full-body{display:flex;flex-direction:column;gap:6px;min-width:0;width:100%}
.bd-r2v-full-warn{
  font-size:10px;line-height:1.45;color:#e8a23a;
  background:#2a2010;border:1px solid #5a4530;border-radius:4px;padding:5px 8px
}
.bd-r2v-final{display:flex;flex-direction:column;gap:4px;min-width:0}
.bd-r2v-final-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
.bd-r2v-final-title{font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#888}
.bd-r2v-final-copy{
  padding:2px 8px;font-size:10px;border-radius:4px;cursor:pointer;
  border:1px solid #444;background:#252525;color:#ddd;transition:all .15s
}
.bd-r2v-final-copy:hover{border-color:#4fff8f;color:#4fff8f}
.bd-r2v-final-pre{
  margin:0;padding:8px;max-height:220px;overflow:auto;
  background:#101010;border:1px solid #2e2e2e;border-radius:6px;
  color:#c8ffd9;font-size:11px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  line-height:1.5;white-space:pre-wrap;word-break:break-word;user-select:text
}

/* Legacy prompts predating the six-section format parse to nothing; without this
   strip the panel reads as "all empty" and looks like data loss. */
.r2v-sections-notice{
  font-size:10px;line-height:1.45;color:#9ab8e8;
  background:#101a2a;border:1px solid #2a3a5a;border-radius:4px;padding:5px 8px
}
.r2v-sections-notice.hidden{display:none}`;

let stylesInjected = false;

function injectStyles() {
    if (stylesInjected) return;
    stylesInjected = true;
    const el = document.createElement("style");
    el.textContent = SECTION_STYLES;
    document.head.appendChild(el);
}

// ─── Parsing & Assembly ──────────────────────────────────────────────────────

/**
 * Parse a combined r2v prompt into six sections.
 * Supports both JSON format (§8.1) and plain-text six-section format (§8.2).
 *
 * @param {string} input - Combined prompt text or JSON string
 * @returns {Object|null} - {section_name: content} or null if parse fails
 */
export function parseR2vSections(input) {
    const text = String(input || "").trim();
    if (!text) return null;

    // Try JSON format first (§8.1 recommended)
    try {
        const data = JSON.parse(text);
        if (data && typeof data === "object") {
            // Check if all six sections present
            if (SECTION_NAMES.every(name => name in data)) {
                const result = {};
                for (const name of SECTION_NAMES) {
                    result[name] = String(data[name] || "").trim();
                }
                return result;
            }
            // Partial JSON: return what we have
            const result = {};
            let hasAny = false;
            for (const name of SECTION_NAMES) {
                if (name in data) {
                    result[name] = String(data[name] || "").trim();
                    hasAny = true;
                } else {
                    result[name] = "";
                }
            }
            return hasAny ? result : null;
        }
    } catch (e) {
        // Not JSON, continue to plain-text parsing
    }

    // Plain-text six-section format (§8.2)
    // Pattern: section_name: content (or section_name:\ncontent)
    // Strategy: find all section headers, extract content between them.
    const result = {};
    let hasAny = false;
    
    // Build a regex to find all section headers
    const headerPattern = new RegExp(
        `^(${SECTION_NAMES.join("|")}):\\s*$`,
        "gim"
    );
    
    // Find all header positions
    const headers = [];
    let match;
    while ((match = headerPattern.exec(text)) !== null) {
        headers.push({
            name: match[1].toLowerCase(),
            start: match.index,
            contentStart: match.index + match[0].length,
        });
    }
    
    // Extract content between headers
    for (let i = 0; i < headers.length; i++) {
        const header = headers[i];
        const nextHeader = headers[i + 1];
        const contentEnd = nextHeader ? nextHeader.start : text.length;
        const content = text.slice(header.contentStart, contentEnd).trim();
        // Normalize section name to match SECTION_NAMES casing
        const normalizedName = SECTION_NAMES.find(n => n.toLowerCase() === header.name);
        if (normalizedName) {
            result[normalizedName] = content;
            hasAny = true;
        }
    }
    
    // Fill missing sections with empty string
    for (const name of SECTION_NAMES) {
        if (!(name in result)) {
            result[name] = "";
        }
    }

    // Fallback: if no sections matched, try to detect by content patterns
    if (!hasAny) {
        // Check for [Shot N] pattern → detailed_description
        if (/\[Shot \d+\]/i.test(text)) {
            result.detailed_description = text;
            hasAny = true;
        }
        // Check for <Subject N> pattern → subject_definitions
        else if (/<Subject \d+>/i.test(text)) {
            result.subject_definitions = text;
            hasAny = true;
        }
    }

    return hasAny ? result : null;
}

/**
 * Assemble six sections into a combined prompt text (plain-text format §8.2).
 *
 * @param {Object} sections - {section_name: content}
 * @param {boolean} useJson - If true, output JSON format (§8.1)
 * @returns {string} - Combined prompt text
 */
export function assembleR2vSections(sections, useJson = false) {
    if (!sections || typeof sections !== "object") return "";

    if (useJson) {
        const obj = {};
        for (const name of SECTION_NAMES) {
            obj[name] = String(sections[name] || "").trim();
        }
        return JSON.stringify(obj, null, 2);
    }

    // Plain-text format: section_name:\ncontent\n\n
    const parts = [];
    for (const name of SECTION_NAMES) {
        const content = String(sections[name] || "").trim();
        if (content) {
            parts.push(`${name}:\n${content}`);
        }
    }
    return parts.join("\n\n");
}

/**
 * Split sections into "common" (first 3) and "segment" (last 3) groups.
 * Per spec §9.1: common = subject_definitions + summary + retention_analysis + style opening
 *              segment = detailed_description + overall_soundscape + non_diegetic_music
 *
 * @param {Object} sections - {section_name: content}
 * @returns {{common: Object, segment: Object}}
 */
export function splitSectionsForDirector(sections) {
    if (!sections) return { common: {}, segment: {} };

    const pick = (names) => {
        const out = {};
        for (const n of names) out[n] = sections[n] || "";
        return out;
    };

    return { common: pick(COMMON_SECTIONS), segment: pick(SEGMENT_SECTIONS) };
}

// ─── Final Prompt Mirror ─────────────────────────────────────────────────────
// The backend rewrites seg.prompt twice before it reaches the model. Mirroring
// both steps keeps the read-only preview honest instead of approximate. Lives in
// its own dependency-free module so it can be diffed against the Python
// originals directly under node; re-exported here for discoverability.
export {
    buildR2vFinalPrompt,
    concatCommonSegmentPrompt,
    reinforceR2vPrompt,
} from "./minimax_r2v_final_prompt.js";

// ─── Validation ──────────────────────────────────────────────────────────────

/**
 * Validate six sections and return status per section.
 *
 * @param {Object} sections - {section_name: content}
 * @returns {Object} - {section_name: {filled: boolean, wordCount: number, warnings: string[]}}
 */
export function validateR2vSections(sections) {
    const result = {};

    for (const name of SECTION_NAMES) {
        const content = String(sections?.[name] || "").trim();
        const words = countWords(content);
        const warnings = [];

        // Check for abstract words (official SKILL.md line 39)
        const abstractPattern = /\b(cinematic|beautiful|stunning|amazing|awesome|gorgeous|perfect|incredible)\b/gi;
        const abstractMatches = content.match(abstractPattern);
        if (abstractMatches) {
            warnings.push(`abstract_words: ${[...new Set(abstractMatches.map(m => m.toLowerCase()))].join(", ")}`);
        }

        // Check detailed_description word count (target 350-500)
        if (name === "detailed_description" && words > 0) {
            if (words < 350) warnings.push(`word_count_low: ${words}/350`);
            if (words > 500) warnings.push(`word_count_high: ${words}/500`);
        }

        // Check for [Shot N] in detailed_description
        if (name === "detailed_description" && content && !/\[Shot \d+\]/i.test(content)) {
            warnings.push("missing_shot_marker");
        }

        // Check for <Subject N> in subject_definitions
        if (name === "subject_definitions" && content && !/<Subject \d+>/i.test(content)) {
            warnings.push("missing_subject_label");
        }

        result[name] = {
            filled: words > 0,
            wordCount: words,
            warnings,
        };
    }

    return result;
}

// ─── UI Component ────────────────────────────────────────────────────────────

/**
 * Show a modal dialog to import an AI-generated r2v script.
 * Standalone (no panel dependency): parses the pasted text and hands the
 * resulting six-section object to onImport. Errors are shown inline.
 * @param {(sections: Object) => void} onImport
 */
function showR2vImportDialog(onImport) {
    const overlay = document.createElement("div");
    Object.assign(overlay.style, {
        position: "fixed", top: "0", left: "0", right: "0", bottom: "0",
        background: "rgba(0,0,0,.7)", zIndex: "10000",
        display: "flex", alignItems: "center", justifyContent: "center",
    });
    const dialog = document.createElement("div");
    Object.assign(dialog.style, {
        background: "#1a1a1a", border: "1px solid #333", borderRadius: "8px",
        padding: "16px", width: "90%", maxWidth: "600px", maxHeight: "80vh",
        display: "flex", flexDirection: "column", gap: "12px",
    });
    const title = document.createElement("div");
    Object.assign(title.style, { fontSize: "14px", fontWeight: "600", color: "#4fff8f" });
    title.textContent = t("r2v.import.title") || "🤖 导入 AI 剧本";
    const hint = document.createElement("div");
    Object.assign(hint.style, { fontSize: "11px", color: "#888", lineHeight: "1.4", whiteSpace: "pre-wrap" });
    hint.textContent = t("r2v.import.hint") || "";
    const textarea = document.createElement("textarea");
    Object.assign(textarea.style, {
        width: "100%", minHeight: "200px", padding: "10px", boxSizing: "border-box",
        background: "#12151b", color: "#d6dbe6", border: "1px solid #2a3140",
        borderRadius: "4px", fontSize: "11px", fontFamily: "monospace",
        resize: "vertical", outline: "none",
    });
    textarea.placeholder = t("r2v.import.placeholder") || "";
    const errorEl = document.createElement("div");
    Object.assign(errorEl.style, { fontSize: "11px", color: "#ff6b6b", minHeight: "14px" });
    const btnRow = document.createElement("div");
    Object.assign(btnRow.style, { display: "flex", gap: "8px", justifyContent: "flex-end" });
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    Object.assign(cancelBtn.style, {
        padding: "6px 16px", background: "#333", color: "#ddd",
        border: "1px solid #444", borderRadius: "4px", cursor: "pointer", fontSize: "11px",
    });
    cancelBtn.textContent = t("r2v.import.cancel") || "取消";
    cancelBtn.onclick = () => overlay.remove();
    const importBtn = document.createElement("button");
    importBtn.type = "button";
    Object.assign(importBtn.style, {
        padding: "6px 16px", background: "#8b5cf6", color: "#fff",
        border: "none", borderRadius: "4px", cursor: "pointer", fontSize: "11px", fontWeight: "600",
    });
    importBtn.textContent = t("r2v.import.confirm") || "导入并填充";
    importBtn.onclick = () => {
        const input = textarea.value.trim();
        if (!input) {
            errorEl.textContent = t("r2v.import.empty") || "请输入剧本内容";
            return;
        }
        const parsed = parseR2vSections(input);
        if (!parsed) {
            errorEl.textContent = t("r2v.import.parseError") || "无法解析剧本格式";
            return;
        }
        if (typeof onImport === "function") onImport(parsed);
        overlay.remove();
    };
    btnRow.appendChild(cancelBtn);
    btnRow.appendChild(importBtn);
    dialog.appendChild(title);
    dialog.appendChild(hint);
    dialog.appendChild(textarea);
    dialog.appendChild(errorEl);
    dialog.appendChild(btnRow);
    overlay.appendChild(dialog);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
    textarea.focus();
}

/**
 * Create the six-section editor UI.
 *
 * @param {Object} options
 * @param {HTMLElement} options.container - Parent element to mount into
 * @param {Function} options.onGetPrompt - () => string, get current prompt text
 * @param {Function} options.onSetPrompt - (text: string) => void, set prompt text
 * @param {Function} [options.onSplitToDirector] - (common, segment) => void, optional split handler
 * @returns {Object} - Editor API { refresh, getSections, setSections, destroy }
 */
export function createR2vSectionsEditor(options) {
    const {
        container,
        onGetPrompt,
        onSetPrompt,
        onSplitToDirector,
        sectionNames,
        editorHost,
        getMedia,
    } = options;
    if (!container) return null;

    injectStyles();

    // Active sections: a subset (common = first 3, segment = last 3) or all six.
    const activeNames = (Array.isArray(sectionNames) && sectionNames.length)
        ? sectionNames
        : SECTION_NAMES;

    // State
    let sections = {};
    let isCollapsed = false;
    let syncTimer = null;
    // Set once the user actually changes section content. flush()/destroy() are
    // no-ops while it is false: an untouched editor holds whatever the parser
    // could recognise, so writing it back would erase unparseable prompt text.
    let dirty = false;
    // Set when the folded raw-prompt view takes over authority. Sections are then
    // deliberately stale: neither flushing them nor re-parsing the raw text back
    // is allowed (that would break the "never flows back" contract).
    let rawEdited = false;
    // The canonical text this editor currently mirrors, so an untouched editor can
    // tell "nothing changed" from "the workflow was swapped under me".
    let sourceText = "";

    // Build DOM
    const wrapper = document.createElement("div");
    wrapper.className = "r2v-sections-wrapper";

    // Toggle button
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "r2v-sections-toggle";
    toggle.innerHTML = `<span class="icon">📋</span><span>${t("r2v.sections.toggle") || "六段式编辑器"}</span>`;
    toggle.addEventListener("click", () => {
        isCollapsed = !isCollapsed;
        wrapper.classList.toggle("r2v-sections-collapsed", isCollapsed);
        toggle.classList.toggle("active", !isCollapsed);
    });
    wrapper.appendChild(toggle);

    // Panel
    const panel = document.createElement("div");
    panel.className = "r2v-sections-panel";

    // Header
    const header = document.createElement("div");
    header.className = "r2v-sections-header";
    header.innerHTML = `
        <div class="r2v-sections-title">
            <span>📋</span>
            <span>${t("r2v.sections.title") || "r2v 六段式结构"}</span>
        </div>
        <div class="r2v-sections-actions">
            <button type="button" class="r2v-section-btn" data-action="insert-template"
                    title="${t("r2v.btn.insertTemplate") || "插入六段式模板"}">
                ${t("r2v.sections.templateBtn") || "📋 模板"}
            </button>
            <button type="button" class="r2v-section-btn" data-action="import-script"
                    title="${t("r2v.btn.importScript") || "导入 AI 剧本"}">
                ${t("r2v.sections.importBtn") || "🤖 导入"}
            </button>
            <button type="button" class="r2v-section-btn" data-action="sync-from-prompt"
                    title="${t("r2v.sections.syncFromPrompt") || "从提示词同步"}">
                ↓ ${t("r2v.sections.syncFrom") || "从提示词"}
            </button>
            <button type="button" class="r2v-section-btn primary" data-action="sync-to-prompt"
                    title="${t("r2v.sections.syncToPrompt") || "同步到提示词"}">
                ↑ ${t("r2v.sections.syncTo") || "到提示词"}
            </button>
            ${onSplitToDirector ? `
            <button type="button" class="r2v-section-btn" data-action="split-to-director"
                    title="${t("r2v.sections.splitHint") || "拆分到公共+分组"}">
                ✂️ ${t("r2v.sections.split") || "拆分"}
            </button>` : ""}
        </div>
    `;
    panel.appendChild(header);

    // Legacy-prompt strip: shown only when canonical text exists but none of it
    // landed in this editor's sections.
    const notice = document.createElement("div");
    notice.className = "r2v-sections-notice hidden";
    notice.setAttribute("data-i18n", "r2v.sections.legacyNotice");
    notice.textContent = t("r2v.sections.legacyNotice");
    notice.hidden = true;
    panel.appendChild(notice);

    // Section editors
    const sectionEditors = {};
    for (const name of activeNames) {
        const item = document.createElement("div");
        item.className = "r2v-section-item";
        item.style.setProperty("--section-color", SECTION_COLORS[name]);

        const itemHeader = document.createElement("div");
        itemHeader.className = "r2v-section-item-header";

        const label = document.createElement("div");
        label.className = "r2v-section-label";
        label.innerHTML = `<span class="dot"></span><span>${t(SECTION_LABELS[name]) || name}</span>`;

        const badge = document.createElement("span");
        badge.className = "r2v-section-badge empty";
        badge.textContent = t("r2v.sections.empty") || "空";

        itemHeader.appendChild(label);
        itemHeader.appendChild(badge);

        const textarea = document.createElement("textarea");
        textarea.className = "r2v-section-textarea";
        textarea.placeholder = SECTION_HINTS[name] || "";
        textarea.dataset.section = name;

        // Debounced sync on input
        textarea.addEventListener("input", () => {
            sections[name] = textarea.value;
            // Sections just reasserted authority over the raw view.
            dirty = true;
            rawEdited = false;
            updateBadge(name, badge);
            updateLegacyNotice();
            scheduleSyncToPrompt();
        });

        item.appendChild(itemHeader);
        item.appendChild(textarea);
        panel.appendChild(item);

        // @-mentions + official-tag chips, same surface as the raw prompt editor.
        // Must run after the textarea is in the DOM (ensureTokenShell reparents).
        if (typeof getMedia === "function") {
            wirePromptImageMentions(editorHost, textarea, getMedia, { compact: true });
        }

        sectionEditors[name] = { textarea, badge, item };
    }

    // Footer with stats
    const footer = document.createElement("div");
    footer.className = "r2v-sections-footer";
    footer.innerHTML = `
        <div class="r2v-sections-stats">
            <div class="r2v-sections-stat">
                <span>${t("r2v.sections.filled") || "已填"}:</span>
                <span class="value" data-stat="filled">0/${activeNames.length}</span>
            </div>
            <div class="r2v-sections-stat">
                <span>${t("r2v.sections.words") || "词数"}:</span>
                <span class="value" data-stat="words">0</span>
            </div>
        </div>
        <div class="r2v-sections-hint">
            ${t("r2v.sections.hint") || "官方六段式顺序不可变"}
        </div>
    `;
    panel.appendChild(footer);

    wrapper.appendChild(panel);
    container.appendChild(wrapper);

    // ─── Internal helpers ────────────────────────────────────────────────────

    /** Keep only this editor's active sections (subset-safe: common=first 3, segment=last 3). */
    function assignActive(src) {
        const out = {};
        for (const name of activeNames) out[name] = (src && src[name]) || "";
        return out;
    }

    /** Replace sections (filtered to activeNames), refresh textareas + badges. */
    function applySections(newSections, markDirty = true) {
        sections = assignActive(newSections);
        if (markDirty) dirty = true;
        for (const name of activeNames) {
            const editor = sectionEditors[name];
            if (editor) {
                editor.textarea.value = sections[name] || "";
                updateBadge(name, editor.badge);
            }
        }
    }

    function updateBadge(name, badge) {
        const content = String(sections[name] || "").trim();
        const words = countWords(content);
        if (words > 0) {
            badge.className = "r2v-section-badge filled";
            badge.textContent = `${words} ${t("r2v.sections.words") || "词"}`;
        } else {
            badge.className = "r2v-section-badge empty";
            badge.textContent = t("r2v.sections.empty") || "空";
        }
        updateStats();
    }

    function updateStats() {
        const filled = activeNames.filter(n => String(sections[n] || "").trim()).length;
        const totalWords = activeNames.reduce((sum, n) => sum + countWords(sections[n]), 0);
        const filledEl = footer.querySelector('[data-stat="filled"]');
        const wordsEl = footer.querySelector('[data-stat="words"]');
        if (filledEl) filledEl.textContent = `${filled}/${activeNames.length}`;
        if (wordsEl) wordsEl.textContent = String(totalWords);
    }

    /**
     * Pre-six-section prompts (or ones whose only match fell into a section this
     * editor does not own) parse to nothing here. Say so, and point at the folded
     * raw view, instead of showing six "empty" badges that read as data loss.
     */
    function updateLegacyNotice() {
        const canonical = String(typeof onGetPrompt === "function" ? onGetPrompt() : "").trim();
        const allEmpty = activeNames.every((n) => !String(sections[n] || "").trim());
        const show = !!canonical && allEmpty;
        notice.hidden = !show;
        notice.classList.toggle("hidden", !show);
    }

    function scheduleSyncToPrompt() {
        clearTimeout(syncTimer);
        syncTimer = setTimeout(() => {
            const text = assembleR2vSections(sections, false);
            sourceText = text;
            if (typeof onSetPrompt === "function") {
                onSetPrompt(text);
            }
        }, 300);
    }

    function syncFromPrompt() {
        const text = typeof onGetPrompt === "function" ? onGetPrompt() : "";
        // Recorded even when parsing fails, so isStale() does not retry forever on
        // free-form prompts the parser cannot split.
        sourceText = String(text ?? "");
        const parsed = parseR2vSections(text);
        // Pulling canonical text in is not a user edit — the parser only
        // recognises its own formats, so echoing the result back could drop
        // whatever the prompt said before.
        if (parsed) applySections(parsed, false);
        updateLegacyNotice();
    }

    function syncToPrompt() {
        readTextareasIntoSections(sectionEditors, sections);
        const text = assembleR2vSections(sections, false);
        sourceText = text;
        if (typeof onSetPrompt === "function") {
            onSetPrompt(text);
        }
    }

    /**
     * Write the pending debounced sync out now — called before DOM teardown.
     * No-op until the user has actually edited a section, and no-op once the raw
     * view has taken over: an untouched editor only holds what parseR2vSections
     * recognised, so echoing that back would wipe text it does not understand.
     */
    function flushPendingSync() {
        clearTimeout(syncTimer);
        syncTimer = null;
        if (!dirty || rawEdited) return;
        syncToPrompt();
    }

    function splitToDirector() {
        readTextareasIntoSections(sectionEditors, sections);
        const { common, segment } = splitSectionsForDirector(sections);
        if (typeof onSplitToDirector === "function") {
            onSplitToDirector(common, segment);
        }
    }

    /**
     * Where a carry-over belongs: the section meant to hold free-form scene
     * description. detailed_description for segment editors, summary for the
     * common one.
     */
    function carryOverSection() {
        if (activeNames.includes("detailed_description")) return "detailed_description";
        if (activeNames.includes("summary")) return "summary";
        return activeNames[0] || null;
    }

    /**
     * 「📋 模板」 fills blanks; it is not a reset. The six-section editor is the
     * only primary surface in r2v, so replacing what the user already wrote with
     * placeholders would be unrecoverable, and a prompt that predates the format
     * would vanish entirely — the folded raw view mirrors the same canonical
     * field, so it cannot hold the original either.
     * (example_workflows/minimax_h3_director_r2v.json ships exactly that case.)
     */
    function insertTemplate() {
        const hasCommon = activeNames.some(n => COMMON_SECTIONS.includes(n));
        const hasSegment = activeNames.some(n => SEGMENT_SECTIONS.includes(n));
        const template = generateR2vTemplate({
            forCommon: hasCommon && !hasSegment,
            forSegment: hasSegment && !hasCommon,
            subjectCount: 1,
            shotCount: 2,
        });
        const canonical = String(typeof onGetPrompt === "function" ? onGetPrompt() : "").trim();
        // Unrepresented: canonical holds text none of this editor's sections
        // account for — free-form prose from before the format existed, or a
        // six-section prompt whose only matches belong to the other editor.
        // Writing the template would drop it, so it is carried over instead.
        const parsed = canonical ? parseR2vSections(canonical) : null;
        const represented = !!parsed && activeNames.some((n) => String(parsed[n] || "").trim());
        const target = carryOverSection();
        const next = {};
        for (const name of activeNames) {
            const current = String(sections[name] || "").trim();
            next[name] = current || String(template[name] || "");
        }
        if (canonical && !represented && target) {
            // The carry-over section ends up with real content either way, so the
            // template's bracketed hint is dropped there — it would only be noise
            // in what the model receives.
            const current = String(sections[target] || "").trim();
            next[target] = current ? `${current}\n\n${canonical}` : canonical;
        }
        applySections(next);
        syncToPrompt();
    }

    function importScript() {
        showR2vImportDialog((parsed) => {
            applySections(parsed);
            syncToPrompt();
        });
    }

    // ─── Event delegation ────────────────────────────────────────────────────

    panel.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-action]");
        if (!btn) return;
        const action = btn.dataset.action;
        if (action === "sync-from-prompt") syncFromPrompt();
        else if (action === "sync-to-prompt") syncToPrompt();
        else if (action === "split-to-director") splitToDirector();
        else if (action === "insert-template") insertTemplate();
        else if (action === "import-script") importScript();
    });

    // ─── Public API ──────────────────────────────────────────────────────────

    return {
        /** Refresh sections from current prompt text. */
        refresh: syncFromPrompt,

        /** Get current sections object. */
        getSections: () => ({ ...sections }),

        /** Set sections and update UI. */
        setSections: (newSections) => applySections(newSections),

        /** Validate and return status. */
        validate: () => validateR2vSections(sections),

        /** Destroy editor and remove from DOM.
         *  @param {{flush?: boolean}} [opts] - pass flush:false when the whole
         *      panel is going away (node destroy); the canonical field is already
         *      serialised there and writing during teardown would re-arm timers. */
        destroy: ({ flush = true } = {}) => {
            // Flush before teardown: the chip editors' sync() needs live DOM, and
            // a mode switch inside the 300ms debounce would otherwise eat the
            // last keystrokes.
            if (flush) flushPendingSync();
            else { clearTimeout(syncTimer); syncTimer = null; }
            // Drop the chip editors' document/window listeners before the DOM goes.
            teardownPromptImageMentions(wrapper);
            wrapper.remove();
        },

        /** Expand/collapse panel. */
        setCollapsed: (collapsed) => {
            isCollapsed = collapsed;
            wrapper.classList.toggle("r2v-sections-collapsed", isCollapsed);
            toggle.classList.toggle("active", !isCollapsed);
        },

        /** Immediately flush any debounced sync (call before DOM teardown). */
        flush: flushPendingSync,

        /**
         * True when the canonical prompt has moved on without this editor knowing
         * — the workflow was imported, or the task workspace restored underneath
         * it — so refresh() is safe. False while the user is editing sections
         * (would clobber their typing) or while the raw view owns the prompt and
         * canonical still equals what it wrote (would break "never flows back").
         */
        isStale: (currentText) => !dirty && sourceText !== String(currentText ?? ""),

        /** The folded raw-prompt view was edited: sections lose authority, and
         *  its text becomes what this editor mirrors until canonical diverges. */
        noteRawEdit: (text) => {
            dirty = false;
            rawEdited = true;
            sourceText = String(text ?? "");
        },
    };
}

// ─── Collapsible Raw Prompt View ─────────────────────────────────────────────

/**
 * Fold the raw chip editor (the prompt textarea's existing .bd-token-wrap)
 * behind a toggle, so r2v shows the six-section editor as the only primary
 * surface. Edits here write straight to the canonical prompt field and are
 * NEVER parsed back into the sections — hence the warning strip.
 *
 * @param {Object} options
 * @param {HTMLElement} options.tokenWrap - the textarea's .bd-token-wrap; moved into the body
 * @param {Function} [options.getPreviewText] - () => string. When provided, a
 *        read-only "what the model actually receives" block is rendered.
 * @param {string} [options.warnKey="r2v.full.warn"] - i18n key for the warning strip
 * @param {boolean} [options.collapsed=true]
 * @returns {{root: HTMLElement, setCollapsed: Function, refreshPreview: Function}}
 */
export function createR2vFullPromptView(options = {}) {
    const { tokenWrap, getPreviewText, warnKey = "r2v.full.warn", collapsed = true } = options;
    injectStyles();

    const root = document.createElement("div");
    root.className = "bd-r2v-full";

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "bd-r2v-full-toggle";
    const caret = document.createElement("span");
    caret.className = "caret";
    caret.setAttribute("aria-hidden", "true");
    caret.textContent = "\u25b6";
    const toggleLabel = document.createElement("span");
    toggleLabel.setAttribute("data-i18n", "r2v.full.toggle");
    toggleLabel.textContent = t("r2v.full.toggle");
    toggle.append(caret, toggleLabel);

    const body = document.createElement("div");
    body.className = "bd-r2v-full-body";

    const warn = document.createElement("div");
    warn.className = "bd-r2v-full-warn";
    warn.setAttribute("data-i18n", warnKey);
    warn.textContent = t(warnKey);
    body.appendChild(warn);

    if (tokenWrap) body.appendChild(tokenWrap);

    let previewPre = null;
    if (typeof getPreviewText === "function") {
        const box = document.createElement("div");
        box.className = "bd-r2v-final";
        const head = document.createElement("div");
        head.className = "bd-r2v-final-head";
        const title = document.createElement("span");
        title.className = "bd-r2v-final-title";
        title.setAttribute("data-i18n", "r2v.final.title");
        title.textContent = t("r2v.final.title");
        const copyBtn = document.createElement("button");
        copyBtn.type = "button";
        copyBtn.className = "bd-r2v-final-copy";
        copyBtn.setAttribute("data-i18n", "r2v.final.copy");
        copyBtn.textContent = t("r2v.final.copy");
        head.append(title, copyBtn);
        previewPre = document.createElement("pre");
        previewPre.className = "bd-r2v-final-pre";
        box.append(head, previewPre);
        body.appendChild(box);

        copyBtn.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            void copyTextToClipboard(previewPre.textContent || "").then((ok) => {
                if (!ok) return;
                copyBtn.textContent = t("r2v.final.copied");
                setTimeout(() => { copyBtn.textContent = t("r2v.final.copy"); }, 1200);
            });
        });
    }

    root.append(toggle, body);

    function refreshPreview() {
        if (!previewPre || root.classList.contains("collapsed")) return;
        previewPre.textContent = getPreviewText() || "";
    }

    function setCollapsed(next) {
        root.classList.toggle("collapsed", !!next);
        toggle.setAttribute("aria-expanded", next ? "false" : "true");
        if (!next) refreshPreview();
    }

    toggle.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        setCollapsed(!root.classList.contains("collapsed"));
    });

    setCollapsed(collapsed);

    return { root, setCollapsed, refreshPreview };
}

// ─── Template Generation ─────────────────────────────────────────────────────

/**
 * Generate a skeleton template for r2v six-section prompt.
 * Used by "一键填充" (one-click fill) feature.
 *
 * @param {Object} options
 * @param {number} options.subjectCount - Number of subjects (default 1)
 * @param {number} options.pictureCount - Number of pictures (default 0)
 * @param {number} options.videoCount - Number of videos (default 0)
 * @param {number} options.audioCount - Number of audios (default 0)
 * @param {number} options.shotCount - Number of shots (default 1)
 * @param {boolean} options.forCommon - If true, generate only first 3 sections
 * @param {boolean} options.forSegment - If true, generate only last 3 sections
 * @returns {Object} - {section_name: template_content}
 */
export function generateR2vTemplate(options = {}) {
    const {
        subjectCount = 1,
        pictureCount = 0,
        videoCount = 0,
        audioCount = 0,
        shotCount = 1,
        forCommon = false,
        forSegment = false,
    } = options;

    const sections = {};

    // subject_definitions
    if (!forSegment) {
        const subjectLines = [];
        for (let i = 1; i <= subjectCount; i++) {
            subjectLines.push(`<Subject ${i}> [Describe appearance, clothing, distinctive features]`);
        }
        sections.subject_definitions = subjectLines.join("\n");
    }

    // summary
    if (!forSegment) {
        const refs = [];
        if (subjectCount > 0) refs.push(`<Subject 1>`);
        if (pictureCount > 0) refs.push(`<Picture 1>`);
        if (videoCount > 0) refs.push(`<Video 1>`);
        if (audioCount > 0) refs.push(`<Audio 1>`);
        sections.summary = `reference generation: ${refs.join(", ")} [Describe the overall scene and action in 1-2 sentences]`;
    }

    // retention_analysis
    if (!forSegment) {
        const retentionLines = [];
        for (let i = 1; i <= subjectCount; i++) {
            retentionLines.push(`<Subject ${i}> fully_preserved`);
        }
        for (let i = 1; i <= pictureCount; i++) {
            retentionLines.push(`<Picture ${i}> attribute_transfer`);
        }
        for (let i = 1; i <= audioCount; i++) {
            retentionLines.push(`<Audio ${i}> partially_copy`);
        }
        sections.retention_analysis = retentionLines.join("; ");
    }

    // detailed_description
    if (!forCommon) {
        const shotLines = [];
        shotLines.push("The target video is in a [describe visual style] style with [lighting] and [color palette].");
        for (let i = 1; i <= shotCount; i++) {
            if (i === 1) {
                shotLines.push(`[Shot 1] The scene opens [describe opening action]. <Subject 1> [action]. The camera [motion type] with [small|large] amplitude at [slow|fast] speed.`);
            } else {
                const seconds = (i - 1) * 3;
                const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
                const ss = String(seconds % 60).padStart(2, "0");
                shotLines.push(`[Shot ${i}] At ${mm}:${ss}.000, [describe transition and action]. <Subject 1> [action]. The camera [motion type].`);
            }
        }
        sections.detailed_description = shotLines.join("\n");
    }

    // overall_soundscape
    if (!forCommon) {
        sections.overall_soundscape = "[Describe ambient sounds, physical sounds, environmental audio. E.g., City traffic hum, footsteps on pavement, distant sirens]";
    }

    // non_diegetic_music
    if (!forCommon) {
        sections.non_diegetic_music = "[Describe background music: instrumentation, tempo, rhythm, dynamics. E.g., Soft piano melody with strings, slow tempo, gentle dynamics] Or N/A if no music.";
    }

    return sections;
}
