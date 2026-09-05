/** Mirror of the two rewrites the backend applies to an r2v prompt before the
 *  model sees it. Kept dependency-free so it can be diffed against the Python
 *  originals (director/plan.py, director/executor_core.py).
 *
 *  Pipeline: seg.prompt = concat_common_segment_prompt(global, segment)
 *            positive   = reinforce_r2v_prompt(seg.prompt, media indices)
 */

/** Mirror of director/plan.py ``concat_common_segment_prompt``. */
export function concatCommonSegmentPrompt(common, segment) {
    const commonS = String(common ?? "").trim();
    const segmentS = String(segment ?? "").trim();
    if (commonS && segmentS) return `${commonS}\n\n${segmentS}`;
    return commonS || segmentS;
}

/** Mirror of director/plan.py ``reinforce_r2v_prompt``: remind media tags that
 *  the text never mentions. Slots are 0-based, official tags are 1-based.
 *
 *  @param {string} prompt
 *  @param {{refs?: Array, videos?: Array, audios?: Array}} media - items carry
 *      ``index``/``slot``; bare numbers are accepted too.
 */
export function reinforceR2vPrompt(prompt, media = {}) {
    const text = String(prompt ?? "").trim() || "Generate a cinematic scene.";
    const slots = (list) => [...new Set(
        (list || [])
            .map((item) => Number(item?.index ?? item?.slot ?? item))
            .filter((n) => Number.isFinite(n) && n >= 0)
    )].sort((a, b) => a - b);
    // The backend only checks the capitalized and the lowercase spelling.
    const mentioned = (tag) => text.includes(`<${tag}`) || text.includes(`<${tag.toLowerCase()}`);
    const prefix = [];
    const pics = slots(media.refs);
    const vids = slots(media.videos);
    const auds = slots(media.audios);
    if (pics.length && !mentioned("Picture")) prefix.push(pics.map((i) => `<Picture ${i + 1}>`).join(" "));
    if (vids.length && !mentioned("Video")) prefix.push(vids.map((i) => `<Video ${i + 1}>`).join(" "));
    if (auds.length && !mentioned("Audio")) prefix.push(auds.map((i) => `<Audio ${i + 1}>`).join(" "));
    if (!prefix.length) return text;
    return `${prefix.join(" ")} ${text}`;
}

/**
 * Exactly what one r2v group sends to the model.
 * Mirrors gen_timeline.py (concat) + executor_core.py (reinforce).
 *
 * @param {Object} opts
 * @param {string} opts.commonPrompt - timeline.global.prompt (first three sections)
 * @param {string} opts.segPrompt - group prompt (last three sections)
 * @param {boolean} opts.commonEnabled - whether 公共参数 is on
 * @param {{refs?: Array, videos?: Array, audios?: Array}} opts.media - already
 *      merged common + group, the same lists the @-menu is built from
 * @returns {string}
 */
export function buildR2vFinalPrompt({ commonPrompt, segPrompt, commonEnabled, media }) {
    const joined = commonEnabled
        ? concatCommonSegmentPrompt(commonPrompt, segPrompt)
        // gen_timeline.py falls back to `local_prompt or prompt`.
        : (String(segPrompt ?? "").trim() || String(commonPrompt ?? "").trim());
    return reinforceR2vPrompt(joined, media || {});
}
