"""Run MiniMax H3 Director segments through the official ComfyUI core pipeline."""

from __future__ import annotations

import gc
import logging
import os
import time
from typing import Any

import torch

from ..lib.image_prep import assert_minimax_canvas, fit_canvas, fit_video_long_edge
from ..lib.task_modes import SUPPORTED_TASK_KEYS
from ..nodes.conditioning import run_minimax_conditioning
from .core_sampling import sample_single_stage
from .refine_pack import (
    confirm_first_pass_enabled,
    first_pass_sigmas_override,
    refine_needs_canvas,
    refine_passes_for,
    refine_will_sample,
)
from .refine_sampling import apply_segment_refine
from .frame_align import minimax_align_frame_count, pad_or_trim_frames
from .audio_export import (
    AUDIO_MODE_GENERATE,
    AUDIO_MODE_MUTE,
    AUDIO_MODE_SOURCE,
    empty_audio_dict,
    resolve_audio_mode,
)
from .segment_runtime import (
    frames_label,
    resolve_segment_raw_clip,
    segment_passthrough_chunk,
    tensor_frame_to_jpeg_b64,
)
from .plan import (
    DirectorPlan,
    plan_summary,
    prepare_segment_clip,
    resolve_ref_image_size,
    ref_audios_to_dict,
    ref_videos_to_dict,
    reference_video_for_segment,
    refs_to_kwargs_for_context,
    reinforce_r2v_prompt,
    reinforce_rv2v_prompt,
    reinforce_v2v_prompt,
)
from .progress import report_director_finish, report_director_progress, report_director_segment_preview
from .h3_motion_context import (
    DEFAULT_AUDIO_CONTEXT_FRAMES,
    apply_motion_context,
    generation_frame_budget,
    handoff_end_frame,
    snap_context_frames,
    trim_context_prefix,
    trim_export_tail,
)
from .segment_cache import (
    _fingerprint_matches,
    load_first_pass_cache,
    load_first_pass_frames_stale,
    load_segment_audio,
    load_segment_av_latent,
    load_segment_cache,
    load_segment_handoff_meta,
    prune_segment_cache,
    save_first_pass_cache,
    save_segment_cache,
    segment_cache_is_current,
)
from .segment_mp4_export import (
    copy_segment_mp4_suffix,
    maybe_export_segment_mp4,
    maybe_export_segment_mp4s,
    mp4_export_kind,
    new_segment_mp4_run_dir,
)
from .segment_continuity import (
    _StreamingConcatFallback,
    concat_continuous_chunks,
    concat_continuous_chunks_streaming,
    is_continuity_active,
    resolve_prev_segment_output,
)
from .vram_cleanup import cleanup_segment_vram

log = logging.getLogger("ComfyUI-MiniMaxH3-Director.director.core")


# Per-phase memory detail is opt-in (console can get noisy on long runs); peaks
# are always tracked cheaply and surfaced once in the run report.
_MEM_TRACE = os.environ.get("MINIMAX_DIRECTOR_MEM_TRACE", "").strip().lower() in {
    "1", "true", "on", "yes",
}


# 「全部导出」(merge) streams finished segments to the disk cache and frees their
# RAM pixels during the loop, reloading them once at concat. Bounds peak RAM to
# ~1 merged timeline instead of growing with the segment count. Opt-in: the
# default path still holds every segment in RAM exactly as before.
_STREAM_MERGE = os.environ.get("MINIMAX_DIRECTOR_STREAM_MERGE", "").strip().lower() in {
    "1", "true", "on", "yes",
}


def _mem_rss_mb() -> float:
    """Resident set size (system RAM) of this process in MB, dependency-free.

    Linux reads /proc/self/status VmRSS; other platforms fall back to
    resource.getrusage ru_maxrss (bytes on macOS, kB on Linux). Best-effort:
    returns 0.0 when unavailable so diagnostics never break a run.
    """
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0  # kB -> MB
    except Exception:
        pass
    try:
        import resource

        rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return rss / (1024.0 * 1024.0) if rss > 1e7 else rss / 1024.0
    except Exception:
        return 0.0


def _mem_total_mb() -> float:
    """Total system RAM in MB from /proc/meminfo (Linux); 0.0 elsewhere."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return float(line.split()[1]) / 1024.0  # kB -> MB
    except Exception:
        pass
    return 0.0


def _mem_vram_mb() -> tuple[float, float]:
    """(allocated, reserved) CUDA VRAM in MB; (0, 0) when CUDA is unavailable."""
    try:
        if torch.cuda.is_available():
            return (
                torch.cuda.memory_allocated() / (1024.0 * 1024.0),
                torch.cuda.memory_reserved() / (1024.0 * 1024.0),
            )
    except Exception:
        pass
    return 0.0, 0.0


def _note_mem(peaks: dict, phase: str, *, seg: int | None = None, force: bool = False) -> None:
    """Snapshot RAM/VRAM, update running peaks, log.

    Peak tracking is always on (cheap); the per-phase console line is emitted
    only when ``force`` or MINIMAX_DIRECTOR_MEM_TRACE is set, so default runs
    stay quiet while a crash investigation gets full boundaries.
    """
    rss = _mem_rss_mb()
    alloc, reserved = _mem_vram_mb()
    peaks["rss"] = max(peaks.get("rss", 0.0), rss)
    peaks["vram"] = max(peaks.get("vram", 0.0), reserved)
    if _MEM_TRACE or force:
        seg_txt = f" seg={seg}" if seg is not None else ""
        log.info(
            "[MEM]%s %s: RSS=%.0fMB (peak %.0fMB) | VRAM alloc=%.0fMB "
            "reserved=%.0fMB (peak %.0fMB)",
            seg_txt, phase, rss, peaks["rss"], alloc, reserved, peaks["vram"],
        )


def _warn_merge_memory(peaks: dict, chunks, pre_chunks, reports) -> None:
    """Estimate merged-tensor RAM cost and warn before a risky「全部导出」concat.

    Merged export builds a full-timeline float32 tensor (plus a second one for
    images_pre_refine when refine differs) while every per-segment chunk is
    still resident, so the concat peak is ~2x(final + pre). On long multi-group
    videos this is the RAM OOM point; advise 分段导出 before the kernel kills us.
    """
    try:
        if not chunks:
            return
        c0 = chunks[0]
        per_frame = int(c0.shape[1]) * int(c0.shape[2]) * int(c0.shape[3]) * 4
        total_frames = sum(int(c.shape[0]) for c in chunks)
        merged_mb = total_frames * per_frame / (1024.0 * 1024.0)
        pre_differs = (
            bool(pre_chunks)
            and len(pre_chunks) == len(chunks)
            and not all(a is b for a, b in zip(pre_chunks, chunks))
        )
        est_extra_mb = merged_mb * (2.0 if pre_differs else 1.0)
        rss = _mem_rss_mb()
        total_ram = _mem_total_mb()
        peak_est = rss + est_extra_mb
        peaks["merge_est_mb"] = est_extra_mb
        log.info(
            "[MEM] merge estimate: %d frames -> merged ~= %.0fMB; RSS now %.0fMB; "
            "concat peak ~= +%.0fMB (%s).",
            total_frames, merged_mb, rss, est_extra_mb,
            "final + pre-refine" if pre_differs else "final only",
        )
        if total_ram > 0 and peak_est > 0.85 * total_ram:
            msg = (
                f"内存预警：「全部导出」需在 RAM 中再建约 {est_extra_mb:.0f}MB 的整段张量，"
                f"叠加当前 {rss:.0f}MB 预计峰值 {peak_est:.0f}MB，已超过系统内存 "
                f"{total_ram:.0f}MB 的 85%，极可能被 OOM-killer 杀死。"
                "建议改用「分段导出」（逐段写 mp4、不在 RAM 拼接整片），"
                "或减少提示词组数 / 缩短单段时长 / 降低分辨率后重跑。"
            )
            reports.append(msg)
            log.warning("[MEM] %s", msg)
    except Exception as exc:  # diagnostics must never break a run
        log.debug("merge memory estimate skipped: %s", exc)


def _segment_disk_cache_needed(
    plan: DirectorPlan,
    *,
    timeline_seg_total: int,
    will_refine: bool,
    hold_after_first: bool,
) -> bool:
    """Disk cache is for partial re-run / refine / continuity — not single-shot r2v."""
    if will_refine or hold_after_first:
        return True
    if plan.continuity_enabled:
        return True
    if len(plan.segments) > 1 or int(timeline_seg_total) > 1:
        return True
    return False


def _unpack_node_output(out):
    if hasattr(out, "args"):
        args = out.args
        if args:
            return args
    if isinstance(out, (tuple, list)):
        return out
    raise RuntimeError(f"Unexpected node output: {type(out)!r}")


def _decode_av_latent(samples, vae, audio_vae, *, decode_audio: bool = True):
    """Same as official r2v: VAEDecode + VAEDecodeAudio both take the AV latent.

    VAEDecode unbinds the video stream; VAEDecodeAudio unbinds the audio stream.
    Official VAE already writes pixels to ``intermediate_device()`` (CPU by default).
    """
    from nodes import VAEDecode

    images, = VAEDecode().decode(vae, samples)
    if not decode_audio or audio_vae is None:
        return images, empty_audio_dict()
    try:
        from comfy_extras.nodes_audio import VAEDecodeAudio
    except ImportError:
        from comfy_extras.nodes_lt import VAEDecodeAudio  # type: ignore

    audio_out = VAEDecodeAudio.execute(audio_vae, samples)
    audio = _unpack_node_output(audio_out)[0]
    return images, audio


def _trim_decoded_to_export(
    decoded: torch.Tensor,
    audio_dict: dict[str, Any] | None,
    *,
    trim_frames: int,
    export_len: int,
    plan: DirectorPlan,
) -> tuple[torch.Tensor, dict[str, Any] | None]:
    """Drop motion-context prefix and crop to the UI export length."""
    if trim_frames > 0:
        decoded, audio_dict = trim_context_prefix(
            decoded,
            audio_dict,
            trim_frames,
            fps=float(plan.frame_rate or 24),
            match_tail=True,
        )
    if decoded.shape[0] > export_len:
        decoded = decoded[:export_len]
        if isinstance(audio_dict, dict) and audio_dict.get("waveform") is not None:
            sr = int(audio_dict.get("sample_rate") or 32000)
            want = int(round((export_len / float(plan.frame_rate or 24)) * sr))
            wf = audio_dict["waveform"]
            if int(wf.shape[-1]) > want:
                audio_dict = {"waveform": wf[..., :want], "sample_rate": sr}
    return decoded, audio_dict


def _ref_tensor_from_seg_refs(refs, index: int) -> torch.Tensor | None:
    for ref in refs or []:
        if int(getattr(ref, "index", -1)) == index and ref.tensor is not None:
            t = ref.tensor
            if t.shape[0] > 0:
                return t[:1]
    return None


def _build_minimax_inputs(
    plan: DirectorPlan,
    seg,
    *,
    clip_frames: torch.Tensor | None,
    ctx_w: int,
    ctx_h: int,
    prev_tail: torch.Tensor | None,
):
    """Map segment task + refs to MiniMax ImageToVideo / ReferenceToVideo inputs."""
    task_key = seg.task_key
    first_frame = None
    last_frame = None
    ref_images = None
    ref_videos = None
    ref_audios = None
    ref_video_audios = None

    if task_key == "fl2v":
        # Prefer explicit shot refs (index 0=start, 1=end). Official FL2VA allows
        # end-only — never invent a first_frame from the placeholder gen source_video
        # (1×16×16 gray) or a held clip when refs only carry image1.
        first_frame = _ref_tensor_from_seg_refs(seg.refs, 0)
        last_frame = _ref_tensor_from_seg_refs(seg.refs, 1)
        # Empty fl2v shot = text-to-video. Do not invent keyframes from the
        # 1×16×16 gray placeholder or a held clip. End-only also must not
        # promote clip_frames[0] into first_frame.
        if first_frame is not None and last_frame is None and clip_frames is not None:
            # Start+end endpoint hold: last may only live on the clip tail.
            if clip_frames.shape[0] >= 2:
                last_frame = clip_frames[-1:].clone()
    elif task_key == "i2v":
        # Explicit per-segment image wins; motion-context path leaves first_frame empty
        # so the previous tail can be pinned as a multi-frame head instead.
        if clip_frames is not None and clip_frames.shape[0] > 0:
            first_frame = clip_frames[:1]
        else:
            first_frame = _ref_tensor_from_seg_refs(seg.refs, 0)
        del prev_tail
    elif task_key == "r2v":
        ref_kwargs = refs_to_kwargs_for_context(task_key, seg.refs)
        ref_images = {}
        for key, tensor in ref_kwargs.items():
            if tensor is None:
                continue
            idx = key.removeprefix("reference_image_")
            ref_images[f"ref_image_{idx}"] = tensor[:1] if tensor.ndim == 4 else tensor
        if not ref_images:
            ref_images = None
        # Prefer multi-slot ref_videos (r2v batch cards); fall back to legacy single meta.
        ref_videos = ref_videos_to_dict(getattr(seg, "ref_videos", None) or [])
        if not ref_videos:
            nframes = max(5, int(getattr(seg, "frame_count", 0) or plan.total_frames or 124))
            ref_video = reference_video_for_segment(plan, seg, num_frames=nframes)
            if ref_video is not None and ref_video.shape[0] > 0:
                ref_videos = {"ref_video_0": ref_video}
        ref_audios = ref_audios_to_dict(
            getattr(seg, "ref_audios", None) or [],
            cache=getattr(plan, "audio_decode_cache", None),
        )
        ref_video_audios = _ref_video_audios_to_dict(getattr(seg, "ref_video_audios", None) or [])
    elif task_key in {"v2v", "rv2v"}:
        # Bernini-style video edit: each timeline segment's source clip → <Video 1>.
        # rv2v additionally injects 图片1–9 / 音频1–3 as <Picture N> / <Audio J>.
        if clip_frames is None or clip_frames.shape[0] <= 0:
            raise ValueError(
                f"{task_key} segment #{seg.index + 1} has no source frames. "
                "Upload a video in the Director timeline before running."
            )
        ref_videos = {"ref_video_0": clip_frames}
        if task_key == "rv2v":
            # Refs are optional per segment: with refs → <Video 1>+<Picture N>;
            # without refs → same as v2v (source edit only).
            ref_kwargs = refs_to_kwargs_for_context(task_key, seg.refs)
            ref_images = {}
            for key, tensor in ref_kwargs.items():
                if tensor is None:
                    continue
                idx = key.removeprefix("reference_image_")
                ref_images[f"ref_image_{idx}"] = tensor[:1] if tensor.ndim == 4 else tensor
            if not ref_images:
                ref_images = None
            ref_audios = ref_audios_to_dict(
                getattr(seg, "ref_audios", None) or [],
                cache=getattr(plan, "audio_decode_cache", None),
            )

    return first_frame, last_frame, ref_images, ref_videos, ref_audios, ref_video_audios


def _release_segment_file_ref_audios(plan: DirectorPlan, seg) -> None:
    """Drop decoded per-segment file PCM; keep execution-shared/global slots.

    Mux happens after all segments. Unique file slots are rematerialized from
    ``audio_path`` (and may re-decode once). Shared/global objects stay live.
    """
    shared_ids = {id(item) for item in (getattr(plan, "global_ref_audios", None) or [])}
    cache = getattr(plan, "audio_decode_cache", None)
    for item in getattr(seg, "ref_audios", None) or []:
        if id(item) in shared_ids:
            continue
        path = str(getattr(item, "audio_path", "") or "").strip()
        if not path:
            continue
        item.audio = None
        if isinstance(cache, dict):
            cache.pop(path, None)


def _prune_continuity_working_set(
    next_segment_index: int,
    av_latents: dict[int, dict],
    refine_passes: dict[int, list[tuple[str, torch.Tensor]]],
) -> None:
    """Keep only the direct predecessor needed by the next segment.

    Final/pre-refine frames are released separately in「分段导出」after the
    next pin. A missing direct predecessor is loaded from disk cache.
    """
    current = int(next_segment_index)
    keep = current - 1
    for working_set in (av_latents, refine_passes):
        for index in tuple(working_set):
            if int(index) < current and int(index) != keep:
                working_set.pop(index, None)


def _poster_frame(tensor: torch.Tensor | None) -> torch.Tensor:
    """1-frame stand-in so IMAGE list length stays valid after a pixel release."""
    if not isinstance(tensor, torch.Tensor) or tensor.ndim != 4 or int(tensor.shape[0]) <= 0:
        return torch.full((1, 1, 1, 3), 0.5)
    if int(tensor.shape[0]) == 1:
        return tensor.detach().cpu().contiguous()
    return tensor[-1:].detach().cpu().contiguous().clone()


def _release_segment_pixels(
    index: int,
    *,
    completed_outputs: dict[int, torch.Tensor],
    completed_pre_refine: dict[int, torch.Tensor],
    completed_refine_passes: dict[int, list[tuple[str, torch.Tensor]]],
    segment_outputs: list[torch.Tensor],
    segment_pre_refine: list[torch.Tensor],
    progress_pos: dict[int, int],
) -> bool:
    """Drop full-resolution pixels for a finished predecessor. Audio stays.

    Replaces IMAGE-list slots with a 1-frame poster. Safe after mp4 + the
    next segment has already pinned / phase-trimmed this index.
    """
    idx = int(index)
    if idx < 0:
        return False
    chunk = completed_outputs.pop(idx, None)
    pre = completed_pre_refine.pop(idx, None)
    completed_refine_passes.pop(idx, None)
    run_pos = progress_pos.get(idx)
    had = chunk is not None or pre is not None
    if run_pos is not None and run_pos < len(segment_outputs):
        src = chunk if chunk is not None else segment_outputs[run_pos]
        poster = _poster_frame(src)
        segment_outputs[run_pos] = poster
        if run_pos < len(segment_pre_refine):
            if pre is chunk:
                segment_pre_refine[run_pos] = poster
            else:
                segment_pre_refine[run_pos] = _poster_frame(
                    pre if pre is not None else segment_pre_refine[run_pos]
                )
        had = True
    elif chunk is not None or pre is not None:
        had = True
    if had:
        del chunk, pre
        gc.collect()
    return had


def _stream_merge_enabled(plan: DirectorPlan, node_id: str | None) -> bool:
    """True when「全部导出」should stream finished segments through the disk cache.

    Opt-in via MINIMAX_DIRECTOR_STREAM_MERGE; only merge mode with 2+ segments
    (so every segment is already written to the cache by _segment_disk_cache_needed).
    """
    if not _STREAM_MERGE or not node_id:
        return False
    if getattr(plan, "export_mode", "") != "all":
        return False
    return len(getattr(plan, "segments", None) or []) >= 2


def _release_merge_pixels(
    index: int,
    *,
    node_id: str | None,
    plan: DirectorPlan,
    seg_by_index: dict[int, SegmentPlan],
    completed_outputs: dict[int, torch.Tensor],
    completed_pre_refine: dict[int, torch.Tensor],
    output_chunks: list[torch.Tensor],
    output_pre_chunks: list[torch.Tensor],
    output_segments: list,
    segment_outputs: list[torch.Tensor],
    segment_pre_refine: list[torch.Tensor],
    progress_pos: dict[int, int],
    stream_released: set[int],
) -> bool:
    """Free a finished merge-mode segment's RAM pixels once its cache is verified.

    Releases only when the on-disk final cache still matches the segment
    fingerprint (so the concat reload is guaranteed to succeed), then swaps the
    RAM slots for 1-frame posters. When refine is off the pre-refine slot shares
    the SAME poster object so the concat's same_as_final identity check still
    holds and the second concat is skipped. Never releases the predecessor the
    next segment will pin from — the caller keeps current-1 resident.
    """
    idx = int(index)
    if idx < 0 or idx in stream_released:
        return False
    seg = seg_by_index.get(idx)
    if seg is None:
        return False
    if idx in completed_outputs and not _fingerprint_matches(node_id, seg, plan):
        # Cache missing/stale — keep the pixels resident (worst case == current).
        return False
    chunk = completed_outputs.pop(idx, None)
    pre = completed_pre_refine.pop(idx, None)
    if chunk is None and pre is None:
        return False
    poster = _poster_frame(chunk if chunk is not None else pre)
    poster_pre = poster if (pre is None or pre is chunk) else _poster_frame(pre)
    # output_chunks / output_pre_chunks are dense (skipped slots omitted).
    for oi, oseg in enumerate(output_segments):
        if getattr(oseg, "index", -1) == idx:
            if oi < len(output_chunks):
                output_chunks[oi] = poster
            if oi < len(output_pre_chunks):
                output_pre_chunks[oi] = poster_pre
            break
    run_pos = progress_pos.get(idx)
    if run_pos is not None:
        if run_pos < len(segment_outputs):
            segment_outputs[run_pos] = poster
        if run_pos < len(segment_pre_refine):
            segment_pre_refine[run_pos] = poster_pre
    stream_released.add(idx)
    del chunk, pre
    gc.collect()
    return True


def _ref_video_audios_to_dict(items) -> dict | None:
    out: dict = {}
    for item in items or []:
        idx = int(getattr(item, "index", -1))
        audio = getattr(item, "audio", None)
        if idx < 0 or not isinstance(audio, dict) or audio.get("waveform") is None:
            continue
        out[f"ref_video_audio_{idx}"] = audio
    return out or None


def execute_director_plan_core(
    plan: DirectorPlan,
    *,
    node_id: str | None = None,
    model,
    vae,
    audio_vae,
    clip,
    cfg: float = 1.0,
    seed: int = 0,
    steps: int = 25,
    sampler: str = "res_multistep",
    scheduler: str = "simple",
    sigmas=None,
    shift_video: float = 12.0,
    shift_audio: float = 3.0,
    clear_vram_between_segments: bool = True,
) -> tuple[
    torch.Tensor,
    list[torch.Tensor],
    list[dict[str, Any]],
    str,
    list[int],
    torch.Tensor,
    list[torch.Tensor],
    bool,
]:
    """Process every segment with MiniMax H3 conditioning + single-stage sampling."""
    plan.sample_seed = int(seed)
    plan.sample_cfg = float(cfg)
    plan.sample_steps = int(steps)
    plan.sample_sampler = str(sampler or "")
    plan.sample_scheduler = str(scheduler or "")
    first_pass_sigmas = first_pass_sigmas_override(sigmas)
    plan.sample_sigmas = first_pass_sigmas
    plan.sample_sigmas_linked = first_pass_sigmas is not None
    plan.sample_shift_video = float(shift_video)
    plan.sample_shift_audio = float(shift_audio)
    audio_mode = resolve_audio_mode(plan)
    decode_audio = audio_mode == AUDIO_MODE_GENERATE
    # UI toggle on the player bar (timeline.liveTaePreview); default off.
    # When off: skip step TAE and the post-sample full-segment JPEG playback encode.
    raw_live = (plan.raw or {}).get("liveTaePreview", (plan.raw or {}).get("live_tae_preview", False))
    live_tae_preview = raw_live in (True, 1, "1", "true", "True", "on")

    all_segments = plan.segments
    # Drop caches for deleted/shortened timelines. Use every segment index (not
    # run_indices): unselected「选择运行」slots still fill merge/export from disk.
    prune_segment_cache(node_id, [seg.index for seg in all_segments])
    # Strictly honor「选择运行」— never force-sample unselected segments.
    run_indices = plan.run_indices if plan.run_indices is not None else frozenset(range(len(all_segments)))

    run_list = sorted(run_indices)
    seg_total = len(run_list)
    progress_pos = {idx: pos for pos, idx in enumerate(run_list)}
    passthrough_indices: list[int] = []
    # External groups may compact selected packs to 0..N-1 while UI still shows
    # the full group list — prefer original timeline card count for progress UI.
    ext_meta = (plan.raw or {}).get("externalGroups") or {}
    try:
        timeline_seg_total = int(ext_meta.get("count") or 0) or len(all_segments)
    except (TypeError, ValueError):
        timeline_seg_total = len(all_segments)
    timeline_seg_total = max(timeline_seg_total, len(all_segments))

    # Memory diagnostics: track peak RSS (system RAM) + VRAM across the run so a
    # crash on a long multi-group video is attributable to RAM accumulation vs. a
    # per-decode VRAM spike. Detail gated by MINIMAX_DIRECTOR_MEM_TRACE.
    mem_peaks: dict[str, float] = {"rss": 0.0, "vram": 0.0}
    _note_mem(mem_peaks, "run-start", force=True)

    output_chunks: list[torch.Tensor] = []
    output_pre_chunks: list[torch.Tensor] = []
    output_segments: list = []  # plans aligned 1:1 with output_chunks (skips omitted)
    segment_outputs: list[torch.Tensor] = []
    segment_pre_refine: list[torch.Tensor] = []
    segment_audios: list[dict[str, Any]] = []
    skipped_no_cache: list[int] = []
    reports: list[str] = [plan_summary(plan), "", "Execution path: ComfyUI official MiniMax H3"]
    if first_pass_sigmas is not None:
        sigma_steps = max(0, len(first_pass_sigmas) - 1)
        reports.append(
            f"Sample: 外接 SIGMAS（{sigma_steps} 步）→ MiniMaxH3SigmaShift(model) → "
            "BasicGuider/CFGGuider → SamplerCustomAdvanced。"
            "导演台步数/调度器已忽略。"
        )
    else:
        if sigmas is not None:
            reports.append(
                "Sample: 外接 SIGMAS 无效（至少需要 2 个数），回退步数 + 调度器。"
            )
        reports.append(
            "Sample: official MiniMaxH3SigmaShift → BasicScheduler → "
            "BasicGuider/CFGGuider → SamplerCustomAdvanced."
        )
    # One timestamp folder per execute so all segments of this run stay together.
    mp4_run_dir = new_segment_mp4_run_dir(plan)
    if mp4_run_dir is not None:
        reports.append(f"Segment mp4 export dir: {mp4_run_dir}")
    if live_tae_preview:
        reports.append("Live preview: ON — 采样 TAE + 成片后整段 JPEG 播放。")
    else:
        reports.append("Live preview: OFF — 跳过 TAE 与成片 JPEG（节点内不播放）。")
    if clear_vram_between_segments:
        reports.append("VRAM: 段间清理显存已开启（最后一段不清理）。")
    if audio_mode == AUDIO_MODE_MUTE:
        reports.append("Audio: muted — skip audio VAE decode, silent AUDIO output.")
    elif audio_mode == AUDIO_MODE_SOURCE:
        reports.append("Audio: source — skip audio VAE decode, use original timeline audio.")
    else:
        reports.append("Audio: generate — decode MiniMax H3 AV latent audio.")
    selected_ui = ext_meta.get("selected")
    if selected_ui is not None:
        selected_set = {int(x) for x in selected_ui}
        run_ui = [i + 1 for i in sorted(selected_set)]
        skipped = [i + 1 for i in range(timeline_seg_total) if i not in selected_set]
        reports.append(
            f"Run selection: {len(run_list)}/{timeline_seg_total} segment(s) "
            f"(indices {run_ui}; skipped {skipped or 'none'})"
        )
    elif plan.run_indices is not None:
        skipped = [i + 1 for i in range(len(all_segments)) if i not in run_indices]
        reports.append(
            f"Run selection: {len(run_list)}/{len(all_segments)} segment(s) "
            f"(indices {[i + 1 for i in run_list]}; skipped {skipped or 'none'})"
        )

    if plan.continuity_enabled:
        pinned = [
            seg.index + 1
            for seg in all_segments
            if seg.index > 0 and getattr(seg, "continuity_from_prev", True)
        ]
        skipped_pin = [
            seg.index + 1
            for seg in all_segments
            if seg.index > 0 and not getattr(seg, "continuity_from_prev", True)
        ]
        reports.append(
            "Segment continuity: ON — motion context "
            f"{snap_context_frames(plan.continuity_overlap_frames)}f "
            "(pin previous AV tail + trim prefix; t2v/i2v/fl2v/r2v/v2v/rv2v)."
        )
        if pinned:
            reports.append("  Pin from prev: #" + ", #".join(str(i) for i in pinned))
        if skipped_pin:
            reports.append(
                "  Hard cut (per-segment off): #"
                + ", #".join(str(i) for i in skipped_pin)
            )
    else:
        reports.append(
            "Segment continuity: OFF — official MiniMax H3 per-segment path "
            "(no motion-context pin/patch/trim; r2v/v2v/rv2v use stock ReferenceToVideo)."
        )

    completed_outputs: dict[int, torch.Tensor] = {}
    completed_pre_refine: dict[int, torch.Tensor] = {}
    completed_refine_passes: dict[int, list[tuple[str, torch.Tensor]]] = {}
    completed_av_latents: dict[int, dict] = {}
    completed_av_handoff: dict[int, dict] = {}
    completed_audios: dict[int, dict] = {}
    # Segments actually executed by _run_one_segment in THIS run.
    # completed_* also get export-fill hydrations from disk; this set does not.
    resampled_this_run: set[int] = set()
    held_for_confirmation = False
    # True export lengths (post continuity trim). Kept after「分段导出」
    # replaces older IMAGE slots with 1-frame posters.
    segment_export_lengths: dict[int, int] = {}
    export_segments_mode = plan.export_mode == "segments"
    # 「全部导出」streaming merge: free each finished segment's RAM pixels (they
    # are already on disk) and reload them once at concat. Bounds peak RAM to ~1
    # merged timeline instead of growing with the group count. Default OFF.
    stream_merge_active = _stream_merge_enabled(plan, node_id) and not export_segments_mode
    stream_released: set[int] = set()
    seg_by_index: dict[int, SegmentPlan] = {int(s.index): s for s in all_segments}
    if stream_merge_active:
        reports.append(
            "内存优化：「全部导出」流式合并已启用（MINIMAX_DIRECTOR_STREAM_MERGE）——"
            "每段落盘验证后即释放 RAM 像素、拼接时逐段流式重载，"
            "峰值≈1×整片而非随提示词组数线性增长（重载失败自动回退到整片常驻）。"
        )

    def _run_one_segment(
        seg, *, progress_index: int
    ) -> tuple[torch.Tensor, dict[str, Any] | None, torch.Tensor]:
        nonlocal held_for_confirmation
        if seg.task_key not in SUPPORTED_TASK_KEYS:
            raise ValueError(
                f"Task '{seg.task_key}' is not supported on MiniMax H3 Director. "
                f"Supported: {', '.join(sorted(SUPPORTED_TASK_KEYS))}."
            )

        ui_idx = seg.timeline_index
        will_refine = refine_will_sample(plan, seg)
        confirm_first = confirm_first_pass_enabled(plan)
        pre_cache = (
            load_first_pass_cache(node_id, seg, plan)
            if confirm_first and will_refine
            else None
        )
        skip_first_sample = pre_cache is not None
        hold_after_first = confirm_first and will_refine and not skip_first_sample
        held_for_confirmation = held_for_confirmation or hold_after_first
        meta = {
            "frames_label": frames_label(seg),
            "task_key": seg.task_key,
            "timeline_segment_index": ui_idx,
            "timeline_segment_total": timeline_seg_total,
        }

        report_director_progress(
            node_id, segment_index=progress_index, segment_total=seg_total,
            phase="prepare", phase_value=0, phase_max=1, **meta,
        )

        target_len = max(1, int(seg.frame_count or plan.total_frames or 124))
        raw_clip = resolve_segment_raw_clip(plan, seg)

        if seg.source_clip is not None:
            body_raw = seg.source_clip
            target_len = max(target_len, int(body_raw.shape[0]))
        else:
            body_raw = raw_clip[:target_len] if int(raw_clip.shape[0]) > target_len else raw_clip

        if body_raw is not None and body_raw.shape[0] > 0:
            if plan.output_mode == "fixed":
                clip_frames = fit_canvas(body_raw, plan.width, plan.height)
            else:
                # long_edge may leave storage-sized frames (e.g. 496) that are not
                # 32-aligned; lock to the resolved plan canvas after the long-edge fit.
                clip_frames = fit_video_long_edge(body_raw, plan.ref_max_size)
                if (
                    int(clip_frames.shape[1]) != int(plan.height)
                    or int(clip_frames.shape[2]) != int(plan.width)
                ):
                    clip_frames = fit_canvas(clip_frames, plan.width, plan.height)
        else:
            clip_frames = None

        num_frames = minimax_align_frame_count(target_len)
        if clip_frames is not None:
            clip_frames, _ = prepare_segment_clip(clip_frames, num_frames)

        # ── Continuity gate ─────────────────────────────────────────────
        # OFF → official MiniMax H3 path only (no prev load / pin / patch).
        # ON  → after stock conditioning, pin previous AV tail (incl. r2v/v2v/rv2v).
        continuity_active = is_continuity_active(plan, seg)
        prev_tail = None
        prev_av = None
        prev_audio = None
        prev_end_frame = None
        prev_idx = seg.index - 1
        if continuity_active:
            if prev_idx in passthrough_indices:
                raise ValueError(
                    f"段间连贯：片段 #{seg.index + 1} 的前一段 #{prev_idx + 1} "
                    "是源视频透传（未采样/无有效缓存），不能作为 motion context。"
                    "请先运行该段，或将其纳入「选择运行」。"
                )
            prev_seg = all_segments[prev_idx] if prev_idx >= 0 else None
            prev_from_this_run = prev_idx in resampled_this_run
            try:
                prev_tail = resolve_prev_segment_output(
                    plan, all_segments, seg.index, completed_outputs, node_id
                )
            except ValueError as exc:
                # No frame cache for prev (partial re-run, never rendered):
                # continue; AV latent on disk may still pin. If neither
                # exists, skip motion context instead of aborting the queue.
                log.info(
                    "Segment %d continuity resolve failed (%s); "
                    "will pin from AV cache if present.",
                    seg.index + 1,
                    exc,
                )
                prev_tail = None
            # Hydrate prev into completed_* so phase-align trim can rewrite
            # in-memory exports + disk cache even on「分段导出」/ partial re-run
            # (resolve_prev may return a cache tensor without storing it).
            if prev_idx >= 0 and prev_tail is not None and prev_idx not in completed_outputs:
                completed_outputs[prev_idx] = prev_tail
            prev_av = completed_av_latents.get(prev_idx)
            if prev_av is None and prev_seg is not None:
                prev_av = load_segment_av_latent(
                    node_id, prev_seg, plan, allow_stale=True
                )
                if prev_av is not None:
                    completed_av_latents[prev_idx] = prev_av
            prev_handoff = completed_av_handoff.get(prev_idx)
            if prev_handoff is None and prev_seg is not None:
                prev_handoff = load_segment_handoff_meta(
                    node_id, prev_seg, plan, allow_stale=True
                )
                if prev_handoff is not None:
                    completed_av_handoff[prev_idx] = prev_handoff
            prev_audio = completed_audios.get(prev_idx)
            if prev_audio is None and prev_seg is not None:
                prev_audio = load_segment_audio(
                    node_id, prev_seg, plan, allow_stale=True
                )
                if prev_audio is not None:
                    completed_audios[prev_idx] = prev_audio
            if prev_av is None and prev_tail is None:
                reports.append(
                    f"Segment {seg.index + 1}/{timeline_seg_total}: "
                    "上一段无有效缓存，已跳过段间引导"
                    "（重跑上一段或将其纳入「选择运行」可恢复衔接）"
                )
            elif not prev_from_this_run:
                prev_current_era = (
                    prev_seg is not None
                    and segment_cache_is_current(node_id, prev_seg, plan)
                )
                if prev_current_era:
                    reports.append(
                        f"Segment {seg.index + 1}/{timeline_seg_total}: "
                        f"引导接自上一段 #{prev_idx + 1} 的磁盘缓存"
                        "（该段本轮未重跑，但参数与当前一致；接缝对齐成片中的现有结果）"
                    )
                else:
                    reports.append(
                        f"Segment {seg.index + 1}/{timeline_seg_total}: "
                        f"引导接自上一段 #{prev_idx + 1} 的【旧参数时代】磁盘缓存"
                        "（该段参数已变但本轮未重跑；接缝可能不匹配，"
                        "建议重跑上一段或将其纳入「选择运行」）"
                    )
            if prev_handoff:
                prev_end_frame = handoff_end_frame(
                    trim_frames=int(prev_handoff.get("trim_frames") or 0),
                    export_frames=int(prev_handoff.get("export_frames") or 0),
                )
                sample_f = int(prev_handoff.get("sample_frames") or 0)
                # Absolute latent tail is only safe when it equals the export end.
                if sample_f > 0 and prev_end_frame >= sample_f:
                    prev_end_frame = None
            else:
                # Pixel fallback: decoded export has no overshoot beyond the file.
                prev_end_frame = None

        ctx_w = int(plan.width)
        ctx_h = int(plan.height)
        if clip_frames is not None and clip_frames.shape[0] > 0:
            ctx_h, ctx_w = int(clip_frames.shape[1]), int(clip_frames.shape[2])
        # H3 patchify requires W/H multiples of 32 (VAE÷16 then 2×2).
        assert_minimax_canvas(ctx_w, ctx_h)

        report_director_progress(
            node_id, segment_index=progress_index, segment_total=seg_total,
            phase="prepare", phase_value=1, phase_max=1, **meta,
        )

        positive_prompt = seg.prompt

        if seg.task_key == "fl2v":
            from .fl2v_timeline import reinforce_fl2v_prompt

            has_start = any(getattr(r, "index", None) == 0 for r in (seg.refs or []))
            has_end = any(getattr(r, "index", None) == 1 for r in (seg.refs or []))
            if not has_start and not has_end and seg.refs:
                # Legacy packs without explicit indices: [start] or [start, end].
                has_start = True
                has_end = len(seg.refs) >= 2
            positive_prompt = reinforce_fl2v_prompt(
                positive_prompt,
                has_end_frame=has_end,
                has_start_frame=has_start,
            )
        elif seg.task_key == "r2v":
            ref_idxs = [int(getattr(r, "index", 0)) for r in (seg.refs or []) if r is not None]
            vid_idxs = [int(getattr(v, "index", 0)) for v in (getattr(seg, "ref_videos", None) or []) if v is not None]
            audio_idxs = [int(getattr(a, "index", 0)) for a in (seg.ref_audios or []) if a is not None]
            positive_prompt = reinforce_r2v_prompt(
                positive_prompt,
                ref_indices=ref_idxs,
                video_indices=vid_idxs,
                audio_indices=audio_idxs,
            )
        elif seg.task_key == "v2v":
            positive_prompt = reinforce_v2v_prompt(positive_prompt)
        elif seg.task_key == "rv2v":
            ref_idxs = [int(getattr(r, "index", 0)) for r in (seg.refs or []) if r is not None]
            audio_idxs = [int(getattr(a, "index", 0)) for a in (seg.ref_audios or []) if a is not None]
            positive_prompt = reinforce_rv2v_prompt(
                positive_prompt, ref_indices=ref_idxs, audio_indices=audio_idxs,
            )

        report_director_progress(
            node_id, segment_index=progress_index, segment_total=seg_total,
            phase="context_encode", phase_value=0, phase_max=1, **meta,
        )

        # Stock official inputs first (refs / source video / keyframes).
        # prev_tail is unused for r2v/v2v/rv2v here — MC pins after conditioning.
        first_frame, last_frame, ref_images, ref_videos, ref_audios, ref_video_audios = _build_minimax_inputs(
            plan, seg, clip_frames=clip_frames, ctx_w=ctx_w, ctx_h=ctx_h, prev_tail=None,
        )

        # i2v with an explicit new start image = fresh anchor (skip motion context).
        # fl2v keeps last_frame when continuity is on; first_frame yields to context head.
        # r2v/v2v/rv2v: always eligible for MC when continuity_active (refs stay).
        i2v_new_anchor = seg.task_key == "i2v" and first_frame is not None
        use_motion_context = (
            continuity_active
            and not i2v_new_anchor
            and (prev_av is not None or prev_tail is not None)
        )
        if skip_first_sample:
            # Cached first-pass latent already has its original pin; don't rebuild MC.
            use_motion_context = False
        # OFF → context_n=0 → sample_len == official segment length only.
        context_n = snap_context_frames(plan.continuity_overlap_frames) if use_motion_context else 0
        sample_len, _planned_trim = generation_frame_budget(num_frames, context_n)
        if use_motion_context:
            # Clear single-frame first lock so multi-frame context owns the head.
            first_frame = None

        if seg.task_key in {"r2v", "v2v", "rv2v"} and (
            ref_images or ref_videos or ref_audios or ref_video_audios
        ) and audio_vae is None:
            raise ValueError("r2v/v2v/rv2v / reference conditioning requires audio_vae input.")

        # Always build via official MiniMaxH3ImageToVideo / ReferenceToVideo.
        t_cond = time.perf_counter()
        positive, negative, latent, task_hint = run_minimax_conditioning(
            clip=clip,
            vae=vae,
            audio_vae=audio_vae,
            prompt=positive_prompt,
            width=ctx_w,
            height=ctx_h,
            length=sample_len,
            task_key=seg.task_key,
            first_frame=first_frame,
            last_frame=last_frame,
            ref_images=ref_images,
            ref_videos=ref_videos,
            ref_video_audios=ref_video_audios,
            ref_audios=ref_audios,
            ref_image_size=resolve_ref_image_size(seg, plan),
        )
        cond_s = time.perf_counter() - t_cond

        trim_frames = 0
        if use_motion_context:
            # Pin audio from previous AV latent whenever available (official MC path).
            # Do not gate on decode_audio — mute only skips final audio decode.
            pin_audio = (
                audio_mode != AUDIO_MODE_MUTE
                and (prev_av is not None or prev_audio is not None)
            )
            positive, trim_frames, prev_export_trim = apply_motion_context(
                positive,
                latent,
                vae=vae,
                context_length=context_n,
                context_latent=prev_av,
                context_frames=prev_tail,
                # Always pass export audio so a canvas-mismatch fallback
                # (Refine upscale) can still pin audio from the decoded tail.
                context_audio=prev_audio,
                audio_vae=audio_vae,
                continue_audio=pin_audio,
                # t2v/i2v/r2v/v2v/rv2v: context owns the head.
                # fl2v keeps last_frame, marked so origin-shift retiming can move it.
                keep_existing_keyframes=(seg.task_key == "fl2v"),
                context_end_frame=prev_end_frame,
                audio_context_length=DEFAULT_AUDIO_CONTEXT_FRAMES,
            )
            # Phase-align can pin a few frames before the previous export end.
            # Drop that orphaned tail so concat does not replay it at the seam.
            trimmed_prev_export = 0
            if prev_export_trim > 0:
                fps = float(plan.frame_rate or 24)
                prev_chunk = completed_outputs.get(prev_idx)
                if prev_chunk is None and prev_idx >= 0:
                    # Last-resort hydrate (export_mode=segments skipped the prev loop).
                    prev_seg_lazy = next(
                        (s for s in all_segments if s.index == prev_idx), None
                    )
                    if prev_seg_lazy is not None:
                        prev_chunk = load_segment_cache(
                            node_id, prev_seg_lazy, plan, allow_stale=True
                        )
                        if prev_chunk is not None:
                            completed_outputs[prev_idx] = prev_chunk
                            if prev_idx not in completed_audios:
                                lazy_aud = load_segment_audio(
                                    node_id, prev_seg_lazy, plan, allow_stale=True
                                )
                                if lazy_aud is not None:
                                    completed_audios[prev_idx] = lazy_aud
                if prev_chunk is not None:
                    prev_chunk, prev_audio_trim = trim_export_tail(
                        prev_chunk,
                        completed_audios.get(prev_idx),
                        prev_export_trim,
                        fps=fps,
                    )
                    completed_outputs[prev_idx] = prev_chunk
                    segment_export_lengths[prev_idx] = int(prev_chunk.shape[0])
                    if prev_audio_trim is not None:
                        completed_audios[prev_idx] = prev_audio_trim
                    # Lists may already hold the untrimmed tensor/audio from when
                    # the previous segment finished — patch by timeline index.
                    run_pos = progress_pos.get(prev_idx)
                    if run_pos is not None:
                        if run_pos < len(segment_outputs):
                            segment_outputs[run_pos] = prev_chunk
                        if (
                            prev_audio_trim is not None
                            and run_pos < len(segment_audios)
                        ):
                            segment_audios[run_pos] = prev_audio_trim
                    # output_chunks is dense (skipped slots omitted) — match by seg.index.
                    if plan.export_mode == "all":
                        for oi, oseg in enumerate(output_segments):
                            if getattr(oseg, "index", -1) == prev_idx:
                                output_chunks[oi] = prev_chunk
                                break
                    prev_pre = completed_pre_refine.get(prev_idx)
                    if prev_pre is not None:
                        if int(prev_pre.shape[0]) > prev_export_trim:
                            prev_pre, _ = trim_export_tail(
                                prev_pre, None, prev_export_trim, fps=fps
                            )
                        completed_pre_refine[prev_idx] = prev_pre
                        if run_pos is not None and run_pos < len(segment_pre_refine):
                            segment_pre_refine[run_pos] = prev_pre
                        if plan.export_mode == "all":
                            for oi, oseg in enumerate(output_segments):
                                if getattr(oseg, "index", -1) == prev_idx:
                                    if oi < len(output_pre_chunks):
                                        output_pre_chunks[oi] = prev_pre
                                    break
                    # Persist trimmed export so partial re-runs reload the same A/V lengths.
                    # replace_audio=False: do not unlink audio.pt when only video was hydrated.
                    prev_seg = next(
                        (s for s in all_segments if s.index == prev_idx), None
                    )
                    if prev_seg is not None:
                        prev_handoff = dict(completed_av_handoff.get(prev_idx) or {})
                        prev_handoff["export_frames"] = int(prev_chunk.shape[0])
                        prev_handoff["phase_align_trim"] = int(prev_export_trim)
                        completed_av_handoff[prev_idx] = prev_handoff
                        save_segment_cache(
                            node_id,
                            prev_seg,
                            plan,
                            prev_chunk,
                            av_latent=completed_av_latents.get(prev_idx),
                            handoff=prev_handoff,
                            audio=completed_audios.get(prev_idx),
                            replace_audio=False,
                        )
                        # Rewrite incremental mp4 so mid-run files match trimmed length.
                        if hold_after_first:
                            pre_path = maybe_export_segment_mp4(
                                mp4_run_dir,
                                plan,
                                prev_seg,
                                prev_chunk,
                                completed_audios.get(prev_idx),
                                suffix="pre",
                            )
                            mp4_paths = [pre_path] if pre_path else []
                        else:
                            mp4_paths = maybe_export_segment_mp4s(
                                mp4_run_dir,
                                plan,
                                prev_seg,
                                prev_chunk,
                                completed_audios.get(prev_idx),
                                pre_frames=completed_pre_refine.get(prev_idx),
                            )
                        extra_passes = list(completed_refine_passes.get(prev_idx) or [])
                        rewritten_extra: list[tuple[str, torch.Tensor]] = []
                        for suffix, frames in extra_passes:
                            clipped = frames
                            if int(clipped.shape[0]) > prev_export_trim:
                                clipped, _ = trim_export_tail(
                                    clipped, None, prev_export_trim, fps=fps
                                )
                            rewritten_extra.append((suffix, clipped))
                            extra_path = maybe_export_segment_mp4(
                                mp4_run_dir,
                                plan,
                                prev_seg,
                                clipped,
                                completed_audios.get(prev_idx),
                                suffix=suffix,
                            )
                            if extra_path:
                                mp4_paths.append(extra_path)
                        if rewritten_extra:
                            completed_refine_passes[prev_idx] = rewritten_extra
                            last_alias = copy_segment_mp4_suffix(
                                mp4_run_dir,
                                plan,
                                prev_seg,
                                dest_suffix=f"p{len(rewritten_extra) + 1}",
                            )
                            if last_alias:
                                mp4_paths.append(last_alias)
                        for mp4_path in mp4_paths:
                            reports.append(
                                f"Segment {prev_idx + 1}: {mp4_export_kind(mp4_path)} "
                                f"updated after continuity trim → {mp4_path}"
                            )
                    trimmed_prev_export = int(prev_export_trim)
                    log.info(
                        "Director continuity: trimmed %df from seg #%d export "
                        "(phase-align pin gap)",
                        prev_export_trim,
                        prev_idx + 1,
                    )
                else:
                    log.warning(
                        "Director continuity: phase-align wanted to trim %df from "
                        "seg #%d but prev export was unavailable — seam may echo.",
                        prev_export_trim,
                        prev_idx + 1,
                    )
            task_hint = f"{task_hint} + motion context {trim_frames}f"
            reports.append(
                f"Seg #{seg.index + 1}: motion context ON — pin {trim_frames}f "
                f"from seg #{seg.index} "
                f"({'AV latent' if prev_av is not None else 'pixels'}"
                f"{', +audio' if pin_audio else ', video-only'}); "
                f"sample={sample_len}f → export {num_frames}f"
                + (
                    f"; trimmed prev export -{trimmed_prev_export}f (phase pin)"
                    if trimmed_prev_export
                    else ""
                )
            )

        report_director_progress(
            node_id, segment_index=progress_index, segment_total=seg_total,
            phase="context_encode", phase_value=1, phase_max=1, **meta,
        )

        # Single / last segment: skip — official H3 also keeps models loaded.
        if clear_vram_between_segments and seg_total > 1:
            cleanup_segment_vram(enabled=True, unload_models=True)

        def _report_sample_phase(phase: str, value: float) -> None:
            report_director_progress(
                node_id, segment_index=progress_index, segment_total=seg_total,
                phase=phase, phase_value=value, phase_max=1, **meta,
            )

        def _report_step_preview(step: int, total_steps: int, x0) -> None:
            # Live frame for the batch-card preview slot (「生成中…」 area).
            try:
                from .tae_preview import pil_to_jpeg_b64, x0_to_preview_pil

                pil = x0_to_preview_pil(x0, max_side=512)
                if pil is None:
                    return
                report_director_segment_preview(
                    node_id,
                    segment_index=ui_idx,
                    image_b64=pil_to_jpeg_b64(pil),
                    width=pil.width,
                    height=pil.height,
                    live=True,
                    step=step + 1,
                    total_steps=total_steps,
                )
            except Exception as exc:
                log.debug("Live TAE preview skipped: %s", exc)

        t_sample = time.perf_counter()
        if skip_first_sample:
            samples = pre_cache["av_latent"]
            cached_h = pre_cache.get("handoff") or {}
            trim_frames = int(cached_h.get("trim_frames") or 0)
            cached_sample = int(cached_h.get("sample_frames") or 0)
            if cached_sample > 0:
                sample_len = cached_sample
            stale_prev_note = (
                "；本段沿用旧一采（上游段已重抽，不在本次选择范围内），拼接处可能不衔接"
                if pre_cache.get("stale_prev")
                else ""
            )
            reports.append(
                f"Segment {ui_idx + 1}/{timeline_seg_total}: 命中一采缓存 "
                f"(seed={int(getattr(plan, 'sample_seed', seed) or seed)})，跳过一采，开始二采"
                f"{stale_prev_note}"
            )
        else:
            samples = sample_single_stage(
                model=model,
                positive=positive,
                negative=negative,
                latent=latent,
                seed=seed,
                cfg=cfg,
                steps=steps,
                sampler_name=sampler,
                scheduler=scheduler,
                shift_video=shift_video,
                shift_audio=shift_audio,
                sigmas=first_pass_sigmas,
                on_phase=_report_sample_phase,
                on_step_preview=_report_step_preview if live_tae_preview else None,
                preview_every=1,
            )

        first_pass_samples = samples
        first_pass_gpu = None
        pre_export = None
        run_refine = will_refine and not hold_after_first
        if will_refine:
            cached_frames = pre_cache.get("frames") if skip_first_sample else None
            if isinstance(cached_frames, torch.Tensor) and cached_frames.numel() > 0:
                pre_export = cached_frames.detach().cpu().float()
                if run_refine and isinstance(getattr(plan, "refine", None), dict) and refine_needs_canvas(plan.refine):
                    first_pass_gpu = pre_export
            else:
                try:
                    report_director_progress(
                        node_id, segment_index=progress_index, segment_total=seg_total,
                        phase="decode", phase_value=0, phase_max=1, **meta,
                    )
                    first_pass_gpu, _ = _decode_av_latent(
                        samples, vae, audio_vae, decode_audio=False,
                    )
                    pre_export = first_pass_gpu.detach().cpu().float()
                except Exception as exc:
                    log.warning(
                        "Segment %s first-pass decode for images_pre_refine failed (%s).",
                        ui_idx + 1,
                        exc,
                    )
                    first_pass_gpu = None
                    pre_export = None

        pack = getattr(plan, "refine", None)
        upscale_frames = (
            first_pass_gpu
            if run_refine and isinstance(pack, dict) and refine_needs_canvas(pack)
            else None
        )
        if first_pass_gpu is not None and upscale_frames is None:
            del first_pass_gpu
            first_pass_gpu = None
        export_len = int(num_frames) if trim_frames > 0 else int(target_len)
        if will_refine and not skip_first_sample:
            save_first_pass_cache(
                node_id,
                seg,
                plan,
                av_latent=first_pass_samples,
                frames=pre_export,
                handoff={
                    "trim_frames": int(trim_frames),
                    "export_frames": int(export_len),
                    "sample_frames": int(sample_len),
                    "official_mc_length": False,
                },
            )
        pass_clips: list[tuple[str, torch.Tensor]] = []

        def _export_refine_pass(pass_i: int, n_passes: int, latent: dict) -> None:
            if mp4_run_dir is None or int(pass_i) >= int(n_passes):
                return
            suffix = f"p{int(pass_i)}"
            try:
                report_director_progress(
                    node_id, segment_index=progress_index, segment_total=seg_total,
                    phase="decode", phase_value=0, phase_max=1, **meta,
                )
                decoded_p, audio_p = _decode_av_latent(
                    latent, vae, audio_vae, decode_audio=decode_audio,
                )
                decoded_p, audio_p = _trim_decoded_to_export(
                    decoded_p,
                    audio_p,
                    trim_frames=trim_frames,
                    export_len=export_len,
                    plan=plan,
                )
                frames_p = decoded_p.cpu().float()
                del decoded_p
                path = maybe_export_segment_mp4(
                    mp4_run_dir,
                    plan,
                    seg,
                    frames_p,
                    audio_p if isinstance(audio_p, dict) else None,
                    suffix=suffix,
                )
                pass_clips.append((suffix, frames_p))
                if path:
                    reports.append(
                        f"Segment {ui_idx + 1}/{timeline_seg_total}: "
                        f"{mp4_export_kind(path)} saved → {path}"
                    )
            except Exception as exc:
                log.warning(
                    "Segment %s refine pass %d mp4 export failed (%s).",
                    ui_idx + 1,
                    pass_i,
                    exc,
                )

        if run_refine:
            samples, refine_note = apply_segment_refine(
                plan,
                seg,
                samples=samples,
                model=model,
                vae=vae,
                audio_vae=audio_vae,
                positive=positive,
                negative=negative,
                seed=seed,
                cfg=cfg,
                first_steps=steps,
                sampler_name=sampler,
                scheduler=scheduler,
                shift_video=shift_video,
                shift_audio=shift_audio,
                on_phase=_report_sample_phase,
                on_step_preview=_report_step_preview if live_tae_preview else None,
                first_pass_images=upscale_frames,
                trim_frames=trim_frames,
                on_pass=_export_refine_pass if mp4_run_dir is not None else None,
            )
        elif hold_after_first:
            refine_note = (
                f"先确认一采（已缓存 seed={int(getattr(plan, 'sample_seed', seed) or seed)}，未二采；"
                "用同一 seed 再 Queue 将只跑二采）"
            )
        else:
            refine_note = ""
        samples = first_pass_samples if not run_refine else samples
        del upscale_frames
        if first_pass_gpu is not None:
            del first_pass_gpu
            first_pass_gpu = None
        sample_s = time.perf_counter() - t_sample

        report_director_progress(
            node_id, segment_index=progress_index, segment_total=seg_total,
            phase="decode", phase_value=0, phase_max=1, **meta,
        )
        t_decode = time.perf_counter()
        decoded, audio_dict = _decode_av_latent(
            samples, vae, audio_vae, decode_audio=decode_audio,
        )
        # Keep exactly the UI segment length. With motion context, sample is
        # longer (visible+ctx, 17k+5 aligned); after trim, crop to num_frames.
        # Next segment must pin at export end (trim+export), not sample end.
        export_len = int(num_frames) if trim_frames > 0 else int(target_len)
        decoded, audio_dict = _trim_decoded_to_export(
            decoded,
            audio_dict,
            trim_frames=trim_frames,
            export_len=export_len,
            plan=plan,
        )
        report_director_progress(
            node_id, segment_index=progress_index, segment_total=seg_total,
            phase="decode", phase_value=1, phase_max=1, **meta,
        )
        _note_mem(mem_peaks, "post-decode", seg=seg.index)

        chunk = decoded
        if getattr(chunk, "device", None) is not None and chunk.device.type != "cpu":
            chunk = chunk.cpu()
        if chunk.dtype != torch.float32:
            chunk = chunk.float()
        if pre_export is not None:
            pre_export, _ = _trim_decoded_to_export(
                pre_export,
                None,
                trim_frames=trim_frames,
                export_len=export_len,
                plan=plan,
            )
            pre_chunk = pre_export
            if getattr(pre_chunk, "device", None) is not None and pre_chunk.device.type != "cpu":
                pre_chunk = pre_chunk.cpu()
            if pre_chunk.dtype != torch.float32:
                pre_chunk = pre_chunk.float()
        else:
            pre_chunk = chunk
        if hold_after_first and pre_chunk is chunk:
            pre_chunk = chunk.clone()
        decode_s = time.perf_counter() - t_decode
        handoff = {
            "trim_frames": int(trim_frames),
            "export_frames": int(chunk.shape[0]),
            "sample_frames": int(sample_len),
            # False ⇒ next pin must use context_end_frame = trim+export.
            "official_mc_length": False,
        }
        completed_av_latents[seg.index] = samples
        completed_av_handoff[seg.index] = handoff
        if isinstance(audio_dict, dict) and audio_dict.get("waveform") is not None:
            completed_audios[seg.index] = audio_dict
        write_cache = _segment_disk_cache_needed(
            plan,
            timeline_seg_total=timeline_seg_total,
            will_refine=will_refine,
            hold_after_first=hold_after_first,
        )
        t_cache = time.perf_counter()
        if write_cache:
            save_segment_cache(
                node_id,
                seg,
                plan,
                chunk,
                av_latent=samples,
                handoff=handoff,
                audio=audio_dict if isinstance(audio_dict, dict) else None,
            )
        cache_s = time.perf_counter() - t_cache
        completed_outputs[seg.index] = chunk
        completed_pre_refine[seg.index] = pre_chunk
        completed_refine_passes[seg.index] = pass_clips
        segment_export_lengths[seg.index] = int(chunk.shape[0])

        #「分段导出」: flush mp4 as soon as this segment succeeds (crash-safe).
        # Confirmation hold has no final/second-pass clip yet: save only _pre.
        if hold_after_first:
            pre_path = maybe_export_segment_mp4(
                mp4_run_dir,
                plan,
                seg,
                chunk,
                audio_dict if isinstance(audio_dict, dict) else None,
                suffix="pre",
            )
            mp4_paths = [pre_path] if pre_path else []
        else:
            # Final clip = last refine pass; _pre = 一采; _pN = each refine round.
            mp4_paths = maybe_export_segment_mp4s(
                mp4_run_dir,
                plan,
                seg,
                chunk,
                audio_dict if isinstance(audio_dict, dict) else None,
                pre_frames=pre_chunk if run_refine else None,
            )
        n_refine = refine_passes_for(getattr(plan, "refine", None)) if run_refine else 1
        if isinstance(pack, dict) and (pack.get("mode") or "") == "latent_upscale":
            n_refine = 1
        if n_refine > 1:
            last_alias = copy_segment_mp4_suffix(
                mp4_run_dir, plan, seg, dest_suffix=f"p{n_refine}",
            )
            if last_alias:
                mp4_paths.append(last_alias)
        for mp4_path in mp4_paths:
            reports.append(
                f"Segment {ui_idx + 1}/{timeline_seg_total}: "
                f"{mp4_export_kind(mp4_path)} saved → {mp4_path}"
            )

        if (
            live_tae_preview
            and seg.task_key in {"t2v", "i2v", "r2v", "fl2v", "v2v", "rv2v"}
            and decoded.shape[0] >= 1
        ):
            try:
                frames_b64 = [
                    tensor_frame_to_jpeg_b64(decoded[i])
                    for i in range(int(decoded.shape[0]))
                ]
                h, w = int(decoded.shape[1]), int(decoded.shape[2])
                report_director_segment_preview(
                    node_id,
                    segment_index=ui_idx,
                    image_b64=frames_b64[0],
                    width=w,
                    height=h,
                    frames=frames_b64,
                    fps=float(plan.frame_rate or 24),
                )
            except Exception as exc:
                log.debug("Segment video preview skipped: %s", exc)

        if clear_vram_between_segments and progress_index < seg_total - 1:
            cleanup_segment_vram(enabled=True)

        reports.append(
            f"Segment {ui_idx + 1}/{timeline_seg_total}: {task_hint} "
            f"({target_len} frames, seed={seed}"
            f"{', ' + refine_note if refine_note else ''})"
        )
        reports.append(
            f"Segment {ui_idx + 1} timing: "
            f"cond={cond_s:.1f}s sample={sample_s:.1f}s decode={decode_s:.1f}s "
            f"cache={'skipped' if not write_cache else f'{cache_s:.1f}s'} "
            f"cleanup={'skipped' if progress_index >= seg_total - 1 else 'between-seg'}"
        )
        log.info(
            "MiniMax H3 Director segment %d/%d done (%d frames, task=%s)",
            ui_idx + 1, timeline_seg_total, target_len, seg.task_key,
        )
        return chunk, audio_dict, pre_chunk

    def _release_merge(idx: int) -> None:
        """Stream-merge release: free idx's RAM pixels (already verified on disk)."""
        if stream_merge_active:
            _release_merge_pixels(
                idx,
                node_id=node_id,
                plan=plan,
                seg_by_index=seg_by_index,
                completed_outputs=completed_outputs,
                completed_pre_refine=completed_pre_refine,
                output_chunks=output_chunks,
                output_pre_chunks=output_pre_chunks,
                output_segments=output_segments,
                segment_outputs=segment_outputs,
                segment_pre_refine=segment_pre_refine,
                progress_pos=progress_pos,
                stream_released=stream_released,
            )

    for seg in all_segments:
        # AV latent and decoded refine-pass clips are a rolling continuity
        # working set, not final outputs. At the start of segment N, only N-1
        # can still be consumed; older entries have already been persisted.
        _prune_continuity_working_set(
            seg.index,
            completed_av_latents,
            completed_refine_passes,
        )
        if export_segments_mode:
            # Older than the predecessor cannot be pinned anymore.
            for stale in tuple(completed_outputs):
                if int(stale) < int(seg.index) - 1:
                    _release_segment_pixels(
                        stale,
                        completed_outputs=completed_outputs,
                        completed_pre_refine=completed_pre_refine,
                        completed_refine_passes=completed_refine_passes,
                        segment_outputs=segment_outputs,
                        segment_pre_refine=segment_pre_refine,
                        progress_pos=progress_pos,
                    )
        elif stream_merge_active:
            # Same cadence as「分段导出」(prev stays resident for the pin), but
            # pixels are freed to the verified disk cache instead of a poster-only
            # drop — concat reloads them once at the end.
            for stale in tuple(completed_outputs):
                if int(stale) < int(seg.index) - 1:
                    _release_merge(stale)
        if seg.index in run_indices:
            if clear_vram_between_segments and segment_outputs:
                cleanup_segment_vram(enabled=True)
            try:
                chunk, audio_dict, pre_chunk = _run_one_segment(
                    seg, progress_index=progress_pos[seg.index]
                )
            finally:
                _release_segment_file_ref_audios(plan, seg)
            segment_outputs.append(chunk)
            segment_pre_refine.append(pre_chunk)
            segment_audios.append(audio_dict or {})
            segment_export_lengths[seg.index] = int(chunk.shape[0])
            resampled_this_run.add(seg.index)
            _note_mem(mem_peaks, "seg-done", seg=seg.index, force=True)
            if export_segments_mode and seg.index > 0:
                # Next pin + phase-trim already happened inside _run_one_segment.
                _release_segment_pixels(
                    seg.index - 1,
                    completed_outputs=completed_outputs,
                    completed_pre_refine=completed_pre_refine,
                    completed_refine_passes=completed_refine_passes,
                    segment_outputs=segment_outputs,
                    segment_pre_refine=segment_pre_refine,
                    progress_pos=progress_pos,
                )
            if plan.export_mode == "all":
                output_chunks.append(chunk)
                output_pre_chunks.append(pre_chunk)
                output_segments.append(seg)
                if stream_merge_active and seg.index > 0:
                    # seg already pinned + phase-trimmed seg-1 and re-saved its
                    # cache inside _run_one_segment, so seg-1 is now disk-only.
                    _release_merge(seg.index - 1)
            continue

        if plan.export_mode != "all":
            continue

        # Prefer exact cache; pipeline-stale disk render is ok. A different
        # source video is rejected so v2v/rv2v can passthrough the new clip.
        cached = load_segment_cache(node_id, seg, plan)
        used_stale = False
        if cached is None:
            cached = load_segment_cache(node_id, seg, plan, allow_stale=True)
            used_stale = cached is not None
        if cached is not None:
            cached = cached.float()
            completed_outputs[seg.index] = cached
            # images_pre_refine fill prefers the first-pass render (.pre.pt) so
            #「选择运行」re-roll previews merge all-first-pass frames instead of
            # mixing fresh 一采 with cached 二采. Falls back to the final render
            # when no first-pass cache exists (e.g. refine was never connected).
            pre_fill = load_first_pass_frames_stale(
                node_id, seg, plan, match_len=int(cached.shape[0])
            )
            completed_pre_refine[seg.index] = (
                pre_fill if pre_fill is not None else cached
            )
            cached_audio = load_segment_audio(
                node_id, seg, plan, allow_stale=used_stale
            )
            if cached_audio is not None:
                completed_audios[seg.index] = cached_audio
            # Continuity for later sampled segments may need AV latent / handoff.
            cached_av = load_segment_av_latent(
                node_id, seg, plan, allow_stale=used_stale
            )
            if cached_av is not None:
                completed_av_latents[seg.index] = cached_av
            cached_handoff = load_segment_handoff_meta(
                node_id, seg, plan, allow_stale=used_stale
            )
            if cached_handoff is not None:
                completed_av_handoff[seg.index] = cached_handoff
            audio_note = ", +audio" if cached_audio is not None else ", no audio cache"
            stale_note = ", stale fingerprint" if used_stale else ""
            reports.append(
                f"Segment {seg.index + 1}/{len(all_segments)}: loaded from cache "
                f"({cached.shape[0]} frames{audio_note}{stale_note})"
            )
            output_chunks.append(cached)
            output_pre_chunks.append(completed_pre_refine[seg.index])
            output_segments.append(seg)
            # Authoritative export length so a stream-merge release of this
            # (exact-cache) fill can still be sized correctly at concat.
            segment_export_lengths[seg.index] = int(cached.shape[0])
            continue

        # Not selected + no cache: v2v/rv2v may fill from source video; gen batch must not
        # splice gray placeholders. If neither works, skip the slot (do not fail the run).
        fill = segment_passthrough_chunk(plan, seg)
        if fill is None:
            skipped_no_cache.append(seg.index + 1)
            reports.append(
                f"Segment {seg.index + 1}/{len(all_segments)}: skipped — no cache "
                "(outside run selection; omitted from merge)"
            )
            continue
        completed_outputs[seg.index] = fill
        completed_pre_refine[seg.index] = fill
        passthrough_indices.append(seg.index)
        reports.append(
            f"Segment {seg.index + 1}/{len(all_segments)}: source passthrough "
            f"({fill.shape[0]} frames, not sampled — outside run selection)"
        )
        output_chunks.append(fill)
        output_pre_chunks.append(fill)
        output_segments.append(seg)
        segment_export_lengths[seg.index] = int(fill.shape[0])

    if passthrough_indices:
        reports.append(
            "Passthrough (not sampled) segment(s) "
            f"{[i + 1 for i in passthrough_indices]} — run selection is honored; "
            "unselected gaps filled from cache/source for「全部导出」."
        )
    if skipped_no_cache:
        reports.append(
            "Skipped segment(s) with no cache "
            f"{skipped_no_cache} — omitted from「全部导出」merge "
            "(勾选重跑或先全跑可补上)."
        )

    if not output_chunks and not segment_outputs:
        raise ValueError("Director plan produced no segments.")

    report_director_finish(node_id, seg_total)
    export_chunks = output_chunks if output_chunks else segment_outputs
    export_pre_chunks = output_pre_chunks if output_pre_chunks else segment_pre_refine
    export_segments = (
        output_segments
        if output_chunks
        else [all_segments[i] for i in sorted(run_indices)]
    )
    # Prefer completed_outputs: motion-context may have trimmed a prev export
    # tail (phase-align pin gap) after that chunk was already appended here.
    for i, seg in enumerate(export_segments):
        patched = completed_outputs.get(seg.index)
        if patched is not None:
            export_chunks[i] = patched
        patched_pre = completed_pre_refine.get(seg.index)
        if patched_pre is not None and i < len(export_pre_chunks):
            export_pre_chunks[i] = patched_pre
    # Aligned to export_chunks only (skipped slots are already omitted).
    export_audios: list[dict[str, Any]] = []
    missing_audio: list[int] = []
    for seg in export_segments:
        aud = completed_audios.get(seg.index)
        if isinstance(aud, dict) and aud.get("waveform") is not None:
            export_audios.append(aud)
        else:
            export_audios.append({})
            missing_audio.append(seg.index + 1)
    if missing_audio and plan.export_mode == "all":
        reports.append(
            "Audio cache missing for segment(s) "
            f"{missing_audio} — those slots are silent in the merge. "
            "Re-run them once (or run all) to refresh audio cache."
        )
    # Authoritative per-segment export lengths for the merge. Released (stream)
    # slots hold 1-frame posters, so prefer segment_export_lengths; fall back to
    # the resident tensor's real length for slots that were never released.
    merge_lengths = [
        int(segment_export_lengths.get(seg.index) or 0) or int(c.shape[0])
        for seg, c in zip(export_segments, export_chunks)
    ]
    if stream_merge_active and stream_released:
        export_frame_counts = list(merge_lengths)
    else:
        export_frame_counts = [int(c.shape[0]) for c in export_chunks]
    # segment_outputs path (分段导出 / image batch): keep run-order audios.
    if plan.export_mode == "all" and output_chunks:
        segment_audios = export_audios
    else:
        segment_audios = [
            completed_audios.get(idx) or (segment_audios[pos] if pos < len(segment_audios) else {})
            for pos, idx in enumerate(run_list)
        ]
        export_frame_counts = [
            int(
                segment_export_lengths.get(idx)
                or (segment_outputs[pos].shape[0] if pos < len(segment_outputs) else 0)
            )
            for pos, idx in enumerate(run_list)
        ]
    if export_segments_mode:
        # Never assemble the full timeline in RAM. Layout uses the segment list
        # (older slots are 1-frame posters after release).
        fallback = torch.full((1, 1, 1, 3), 0.5)
        combined = segment_outputs[-1] if segment_outputs else fallback
        pre_combined = (
            segment_pre_refine[-1]
            if segment_pre_refine
            else combined
        )
        reports.append(
            "Export mode: segments — released prior-segment pixels after mp4 "
            "and continuity pin (no full-timeline concat; IMAGE keeps a 1-frame poster)."
        )
    else:
        pre_source = export_pre_chunks if export_pre_chunks else segment_pre_refine
        if not pre_source:
            pre_source = list(segment_outputs)
        same_as_final = (
            len(pre_source) == len(export_chunks)
            and all(a is b for a, b in zip(pre_source, export_chunks))
        )
        if stream_merge_active and stream_released:
            # Streaming merge: reload each released segment from its verified disk
            # cache exactly once and copy into a pre-allocated buffer, so the concat
            # peak stays ~1 merged timeline instead of ~3x. Any reload/geometry
            # failure falls back to the classic in-RAM path (worst case == current).
            def _merge_chunk(i: int, *, is_pre: bool):
                seg = export_segments[i]
                idx = seg.index
                want = int(merge_lengths[i])
                if idx not in stream_released:
                    held = pre_source[i] if is_pre else export_chunks[i]
                    if (
                        isinstance(held, torch.Tensor)
                        and held.ndim == 4
                        and int(held.shape[0]) == want
                    ):
                        return held
                if is_pre:
                    t = load_first_pass_frames_stale(node_id, seg, plan, match_len=want)
                    if t is None:
                        t = load_segment_cache(node_id, seg, plan)
                else:
                    t = load_segment_cache(node_id, seg, plan)
                if t is None:
                    t = load_segment_cache(node_id, seg, plan, allow_stale=True)
                if t is None:
                    return None
                t = t.float()
                return t if int(t.shape[0]) == want else None

            def _concat(is_pre: bool):
                try:
                    return concat_continuous_chunks_streaming(
                        lambda i: _merge_chunk(i, is_pre=is_pre),
                        len(export_segments),
                        merge_lengths,
                        plan=plan,
                    )
                except _StreamingConcatFallback as exc:
                    log.warning(
                        "Stream-merge concat fell back to in-RAM path (pre=%s): %s",
                        is_pre, exc,
                    )
                    reports.append(
                        f"流式合并回退到整片常驻拼接（pre={is_pre}：{exc}）——"
                        "本次峰值内存与旧版一致，输出不受影响。"
                    )
                    chunks_list = pre_source if is_pre else export_chunks
                    for i, seg in enumerate(export_segments):
                        if seg.index not in stream_released:
                            continue
                        t = _merge_chunk(i, is_pre=is_pre)
                        if t is None:
                            t = torch.full(
                                (
                                    max(1, int(merge_lengths[i])),
                                    int(plan.height),
                                    int(plan.width),
                                    3,
                                ),
                                0.5,
                            )
                            log.error(
                                "Stream-merge reload FAILED for segment %d; gray placeholder used.",
                                seg.index + 1,
                            )
                            reports.append(
                                f"段 {seg.index + 1} 流式重载失败，已用 {int(merge_lengths[i])} 帧灰占位——"
                                "磁盘缓存可能被删除/损坏，请重跑该段。"
                            )
                        chunks_list[i] = t
                    return concat_continuous_chunks(chunks_list, export_segments, plan)

            combined = _concat(is_pre=False)
            pre_combined = combined if same_as_final else _concat(is_pre=True)
            reports.append(
                f"流式合并完成：拼接期逐段从磁盘缓存重载（释放 {len(stream_released)} 段常驻像素），"
                "峰值≈1×整片。"
            )
        else:
            _warn_merge_memory(mem_peaks, export_chunks, export_pre_chunks, reports)
            combined = concat_continuous_chunks(export_chunks, export_segments, plan)
            pre_combined = (
                combined
                if same_as_final
                else concat_continuous_chunks(pre_source, export_segments, plan)
            )
        _note_mem(mem_peaks, "post-concat", force=True)
    _note_mem(mem_peaks, "run-end", force=True)
    reports.append(
        f"内存峰值：RSS {mem_peaks.get('rss', 0.0):.0f}MB / VRAM {mem_peaks.get('vram', 0.0):.0f}MB"
        "（设置环境变量 MINIMAX_DIRECTOR_MEM_TRACE=1 可打印逐段/逐相位明细）。"
    )
    return (
        combined,
        segment_outputs,
        segment_audios,
        "\n".join(reports),
        export_frame_counts,
        pre_combined,
        segment_pre_refine,
        held_for_confirmation,
    )
