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

/** Read all textarea values into sections object (mutates sections). */
function readTextareasIntoSections(sectionEditors, sections) {
    for (const name of SECTION_NAMES) {
        const editor = sectionEditors[name];
        if (editor) {
            sections[name] = editor.textarea.value;
        }
    }
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
.r2v-sections-actions{display:flex;gap:4px}
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
`;

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

    const common = {
        subject_definitions: sections.subject_definitions || "",
        summary: sections.summary || "",
        retention_analysis: sections.retention_analysis || "",
    };

    const segment = {
        detailed_description: sections.detailed_description || "",
        overall_soundscape: sections.overall_soundscape || "",
        non_diegetic_music: sections.non_diegetic_music || "",
    };

    return { common, segment };
}

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
    const { container, onGetPrompt, onSetPrompt, onSplitToDirector } = options;
    if (!container) return null;

    injectStyles();

    // State
    let sections = {};
    let isCollapsed = false;
    let syncTimer = null;

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

    // Section editors
    const sectionEditors = {};
    for (const name of SECTION_NAMES) {
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
            updateBadge(name, badge);
            scheduleSyncToPrompt();
        });

        item.appendChild(itemHeader);
        item.appendChild(textarea);
        panel.appendChild(item);

        sectionEditors[name] = { textarea, badge, item };
    }

    // Footer with stats
    const footer = document.createElement("div");
    footer.className = "r2v-sections-footer";
    footer.innerHTML = `
        <div class="r2v-sections-stats">
            <div class="r2v-sections-stat">
                <span>${t("r2v.sections.filled") || "已填"}:</span>
                <span class="value" data-stat="filled">0/6</span>
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
        const filled = SECTION_NAMES.filter(n => String(sections[n] || "").trim()).length;
        const totalWords = SECTION_NAMES.reduce((sum, n) => sum + countWords(sections[n]), 0);
        const filledEl = footer.querySelector('[data-stat="filled"]');
        const wordsEl = footer.querySelector('[data-stat="words"]');
        if (filledEl) filledEl.textContent = `${filled}/6`;
        if (wordsEl) wordsEl.textContent = String(totalWords);
    }

    function scheduleSyncToPrompt() {
        clearTimeout(syncTimer);
        syncTimer = setTimeout(() => {
            const text = assembleR2vSections(sections, false);
            if (typeof onSetPrompt === "function") {
                onSetPrompt(text);
            }
        }, 300);
    }

    function syncFromPrompt() {
        const text = typeof onGetPrompt === "function" ? onGetPrompt() : "";
        const parsed = parseR2vSections(text);
        if (parsed) {
            sections = { ...parsed };
            for (const name of SECTION_NAMES) {
                const editor = sectionEditors[name];
                if (editor) {
                    editor.textarea.value = sections[name] || "";
                    updateBadge(name, editor.badge);
                }
            }
        }
    }

    function syncToPrompt() {
        readTextareasIntoSections(sectionEditors, sections);
        const text = assembleR2vSections(sections, false);
        if (typeof onSetPrompt === "function") {
            onSetPrompt(text);
        }
    }

    function splitToDirector() {
        readTextareasIntoSections(sectionEditors, sections);
        const { common, segment } = splitSectionsForDirector(sections);
        if (typeof onSplitToDirector === "function") {
            onSplitToDirector(common, segment);
        }
    }

    // ─── Event delegation ────────────────────────────────────────────────────

    panel.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-action]");
        if (!btn) return;
        const action = btn.dataset.action;
        if (action === "sync-from-prompt") syncFromPrompt();
        else if (action === "sync-to-prompt") syncToPrompt();
        else if (action === "split-to-director") splitToDirector();
    });

    // ─── Public API ──────────────────────────────────────────────────────────

    return {
        /** Refresh sections from current prompt text. */
        refresh: syncFromPrompt,

        /** Get current sections object. */
        getSections: () => ({ ...sections }),

        /** Set sections and update UI. */
        setSections: (newSections) => {
            sections = { ...newSections };
            for (const name of SECTION_NAMES) {
                const editor = sectionEditors[name];
                if (editor) {
                    editor.textarea.value = sections[name] || "";
                    updateBadge(name, editor.badge);
                }
            }
        },

        /** Validate and return status. */
        validate: () => validateR2vSections(sections),

        /** Destroy editor and remove from DOM. */
        destroy: () => {
            clearTimeout(syncTimer);
            wrapper.remove();
        },

        /** Expand/collapse panel. */
        setCollapsed: (collapsed) => {
            isCollapsed = collapsed;
            wrapper.classList.toggle("r2v-sections-collapsed", isCollapsed);
            toggle.classList.toggle("active", !isCollapsed);
        },
    };
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
