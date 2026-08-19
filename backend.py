"""Standalone backend for the RoboFit dual-camera scoring preview platform."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


logger = logging.getLogger("scoring_platform")

PLATFORM_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_ROOT = PLATFORM_ROOT.parent / "data"
FRONTEND_ROOT = PLATFORM_ROOT / "frontend"
CAMERA_IDS = ("cam1", "cam2")
PREVIEW_FILENAMES = {camera_id: f"{camera_id}_preview.mp4" for camera_id in CAMERA_IDS}
SETS_FILENAME = "score_annotation.json"
SETS_SCHEMA_VERSION = "1.1.0"
SETS_MAX_SCORE = 5
PART_SETS_FILENAME = "part_sets.json"
# 每个被试的前三个动作固定为热身,只记录起止,不评分
WARMUP_LABELS = ("warm_up1", "warm_up2", "warm_up3")


@dataclass(frozen=True, slots=True)
class FrameRecord:
    frame_index: int
    timestamp: float
    path: Path
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class SubjectFrames:
    date: str
    subject_id: str
    trial_path: Path
    cameras: dict[str, tuple[FrameRecord, ...]]
    timestamps: dict[str, tuple[float, ...]]
    timeline_start: float
    timeline_end: float

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.timeline_end - self.timeline_start)


@dataclass(slots=True)
class PreviewJob:
    status: str = "generating"
    progress: float = 0.0
    message: str = "正在准备预览视频"
    camera_id: str | None = None
    error: str | None = None
    cancel_event: threading.Event | None = None
    task: asyncio.Task[None] | None = None


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


def _safe_segment(value: str, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise HTTPException(status_code=400, detail=f"无效的{label}")
    return value


def _read_camera_frames(trial_path: Path, camera_id: str) -> tuple[FrameRecord, ...]:
    csv_path = trial_path / "timestamps" / f"{camera_id}_rgbd_timestamps.csv"
    if not csv_path.is_file():
        raise HTTPException(status_code=404, detail=f"缺少 {camera_id} 时间戳文件")

    frames: list[FrameRecord] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            relative_path = row.get("color_raw_path") or row.get("color_path")
            if not relative_path:
                continue
            frame_path = trial_path / relative_path
            try:
                frame_index = int(row.get("frame_index", len(frames)))
                timestamp = float(row["timestamp"])
                width = int(row.get("color_width") or 640)
                height = int(row.get("color_height") or 480)
            except (KeyError, TypeError, ValueError):
                continue
            frames.append(
                FrameRecord(
                    frame_index=frame_index,
                    timestamp=timestamp,
                    path=frame_path,
                    width=width,
                    height=height,
                )
            )

    if not frames:
        raise HTTPException(status_code=404, detail=f"{camera_id} 没有可预览的 RGB 帧")
    frames.sort(key=lambda item: item.timestamp)
    return tuple(frames)


def _find_ffmpeg() -> str | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    environment_binary = Path(sys.executable).resolve().parent / "ffmpeg"
    return str(environment_binary) if environment_binary.is_file() else None


def _read_part_sets_json(path: Path) -> dict[str, list[str]]:
    """读取动作序列表 JSON:被试编号 → 8 个动作代码(固定顺序)。

    该文件由 docs/part_sets.xlsx 一次性生成并提交仓库,运行时不再依赖 Excel。
    读取失败(文件缺失/损坏/格式异常)时告警并返回空表。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("part_sets 序列表读取失败(%s): %s", path, exc)
        return {}
    subjects = payload.get("subjects") if isinstance(payload, dict) else None
    if not isinstance(subjects, dict):
        return {}
    table: dict[str, list[str]] = {}
    for subject_id, codes in subjects.items():
        if isinstance(subject_id, str) and isinstance(codes, list):
            table[subject_id] = [code for code in codes if isinstance(code, str)]
    return table


def create_app(
    data_root: str | Path | None = None,
    cache_root: str | Path | None = None,
    preview_max_seconds: float | None = None,
    annotations_root: str | Path | None = None,
    part_sets_path: str | Path | None = None,
) -> FastAPI:
    configured_root = data_root or os.getenv("SCORING_DATA_ROOT") or DEFAULT_DATA_ROOT
    resolved_data_root = Path(configured_root).expanduser().resolve()
    configured_cache = cache_root or os.getenv("SCORING_CACHE_ROOT") or PLATFORM_ROOT / "cache"
    resolved_cache_root = Path(configured_cache).expanduser().resolve()
    configured_annotations = annotations_root or os.getenv("SCORING_ANNOTATION_ROOT") or PLATFORM_ROOT / "annotations"
    resolved_annotations_root = Path(configured_annotations).expanduser().resolve()
    configured_part_sets = part_sets_path or os.getenv("SCORING_PART_SETS_PATH") or PLATFORM_ROOT / "docs" / PART_SETS_FILENAME
    resolved_part_sets_path = Path(configured_part_sets).expanduser().resolve()
    # 启动时加载一次动作序列表(卡片会实体化进标注 JSON,改表后重启服务即可)
    part_sets = _read_part_sets_json(resolved_part_sets_path)
    ffmpeg_binary = _find_ffmpeg()
    preview_fps = max(1.0, float(os.getenv("SCORING_PREVIEW_FPS", "30")))
    if preview_max_seconds is None:
        preview_max_seconds = max(0.0, float(os.getenv("SCORING_PREVIEW_MAX_SECONDS", "0")))
    jobs: dict[tuple[str, str], PreviewJob] = {}
    sets_lock = threading.Lock()

    app = FastAPI(
        title="RoboFit Scoring Platform",
        version="0.1.0",
        description="独立的双 RGB 相机数据预览与评分平台",
    )
    app.state.data_root = resolved_data_root
    app.state.cache_root = resolved_cache_root
    app.state.annotations_root = resolved_annotations_root
    app.state.part_sets_path = resolved_part_sets_path

    def subject_path(date: str, subject_id: str) -> Path:
        date = _safe_segment(date, "日期")
        subject_id = _safe_segment(subject_id, "被试编号")
        candidate = (resolved_data_root / date / subject_id).resolve()
        if candidate.parent.parent != resolved_data_root or not candidate.is_dir():
            raise HTTPException(status_code=404, detail="没有找到该被试的数据")
        return candidate

    def subject_cache_path(date: str, subject_id: str) -> Path:
        date = _safe_segment(date, "日期")
        subject_id = _safe_segment(subject_id, "被试编号")
        candidate = (resolved_cache_root / date / subject_id).resolve()
        if candidate.parent.parent != resolved_cache_root:
            raise HTTPException(status_code=400, detail="无效的缓存路径")
        return candidate

    def annotation_file() -> Path:
        # 所有被试的标注统一存放在单个文件 annotations/score_annotation.json 中
        return resolved_annotations_root / SETS_FILENAME

    def _subject_annotation_key(date: str, subject_id: str) -> str:
        return f"{_safe_segment(date, '日期')}/{_safe_segment(subject_id, '被试编号')}"

    @lru_cache(maxsize=4)
    def load_subject(date: str, subject_id: str) -> SubjectFrames:
        trial_path = subject_path(date, subject_id)
        cameras = {
            camera_id: _read_camera_frames(trial_path, camera_id)
            for camera_id in CAMERA_IDS
        }
        timestamps = {
            camera_id: tuple(frame.timestamp for frame in frames)
            for camera_id, frames in cameras.items()
        }
        timeline_start = max(times[0] for times in timestamps.values())
        timeline_end = min(times[-1] for times in timestamps.values())
        if timeline_end < timeline_start:
            raise HTTPException(status_code=422, detail="两路相机没有重叠的采集时间段")
        return SubjectFrames(
            date=date,
            subject_id=subject_id,
            trial_path=trial_path,
            cameras=cameras,
            timestamps=timestamps,
            timeline_start=timeline_start,
            timeline_end=timeline_end,
        )

    def _position_timestamp(subject: SubjectFrames, position: int) -> float:
        # 时间轴位置 0..10000 → 公共时间轴秒(两相机重叠窗口)
        return subject.timeline_start + subject.duration_sec * position / 10_000

    def _nearest_record_index(subject: SubjectFrames, camera_id: str, target_timestamp: float) -> int:
        timestamps = subject.timestamps[camera_id]
        record_index = bisect_left(timestamps, target_timestamp)
        if record_index >= len(timestamps):
            return len(timestamps) - 1
        if record_index > 0:
            before = timestamps[record_index - 1]
            after = timestamps[record_index]
            if target_timestamp - before <= after - target_timestamp:
                record_index -= 1
        return record_index

    def preview_status_payload(date: str, subject_id: str) -> dict[str, object]:
        cache_dir = subject_cache_path(date, subject_id)
        job = jobs.get((date, subject_id))
        video_paths = {
            camera_id: cache_dir / filename
            for camera_id, filename in PREVIEW_FILENAMES.items()
        }
        metadata_path = cache_dir / "metadata.json"
        ready = (
            metadata_path.is_file()
            and all(path.is_file() and path.stat().st_size > 0 for path in video_paths.values())
        )
        metadata: dict[str, object] = {}
        if ready and metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {}
        if ready and (job is None or job.status != "generating"):
            status = "ready"
            progress = 1.0
            message = "流畅预览视频已生成"
            error = None
            camera_id = None
        elif job is not None:
            status = job.status
            progress = job.progress
            message = job.message
            error = job.error
            camera_id = job.camera_id
        else:
            status = "absent"
            progress = 0.0
            message = "尚未生成流畅预览视频"
            error = None
            camera_id = None
        return {
            "status": status,
            "progress": round(progress, 4),
            "message": message,
            "camera_id": camera_id,
            "error": error,
            "ffmpeg_available": ffmpeg_binary is not None,
            "cache_bytes": sum(path.stat().st_size for path in video_paths.values() if path.is_file()),
            "duration_sec": metadata.get("duration_sec"),
            "fps": metadata.get("fps", preview_fps),
            "videos": {
                camera_id: f"/api/subjects/{date}/{subject_id}/preview/{camera_id}.mp4"
                for camera_id in CAMERA_IDS
            } if ready else {},
        }

    def generate_preview_sync(
        subject: SubjectFrames,
        cache_dir: Path,
        job: PreviewJob,
    ) -> None:
        if ffmpeg_binary is None:
            raise RuntimeError("没有找到 FFmpeg；请从 env_robfit Conda 环境启动平台")
        cache_dir.mkdir(parents=True, exist_ok=True)
        output_duration = subject.duration_sec
        if preview_max_seconds and preview_max_seconds > 0:
            output_duration = min(output_duration, preview_max_seconds)
        output_frame_count = max(1, int(output_duration * preview_fps))
        if output_frame_count <= 0:
            raise RuntimeError("公共时间轴内没有可生成的视频帧")

        completed_outputs: list[Path] = []
        try:
            for camera_number, camera_id in enumerate(CAMERA_IDS):
                if job.cancel_event and job.cancel_event.is_set():
                    raise InterruptedError("预览视频生成已取消")
                records = subject.cameras[camera_id]
                width, height = records[0].width, records[0].height
                if any(frame.width != width or frame.height != height for frame in records):
                    raise RuntimeError(f"{camera_id} 采集过程中分辨率发生变化，暂不支持生成视频")

                final_path = cache_dir / PREVIEW_FILENAMES[camera_id]
                partial_path = cache_dir / f".{PREVIEW_FILENAMES[camera_id]}.part.mp4"
                partial_path.unlink(missing_ok=True)
                final_path.unlink(missing_ok=True)
                job.camera_id = camera_id
                job.message = f"正在生成 RGB 相机 {camera_number + 1}"
                command = [
                    ffmpeg_binary,
                    "-hide_banner",
                    "-loglevel", "error",
                    "-y",
                    "-f", "rawvideo",
                    "-pixel_format", "rgb24",
                    "-video_size", f"{width}x{height}",
                    "-framerate", f"{preview_fps:g}",
                    "-i", "pipe:0",
                    "-an",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "24",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    str(partial_path),
                ]
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                try:
                    assert process.stdin is not None
                    for output_index in range(output_frame_count):
                        if job.cancel_event and job.cancel_event.is_set():
                            raise InterruptedError("预览视频生成已取消")
                        target_time = subject.timeline_start + output_index / preview_fps
                        record_index = _nearest_record_index(subject, camera_id, target_time)
                        frame_data = records[record_index].path.read_bytes()
                        expected_size = width * height * 3
                        if len(frame_data) != expected_size:
                            raise RuntimeError(
                                f"{camera_id} 第 {records[record_index].frame_index} 帧大小异常"
                            )
                        process.stdin.write(frame_data)
                        if output_index % max(1, int(preview_fps)) == 0:
                            job.progress = (camera_number + output_index / output_frame_count) / len(CAMERA_IDS)
                    process.stdin.close()
                    return_code = process.wait()
                    error_output = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
                    if return_code != 0:
                        raise RuntimeError(error_output.strip() or f"FFmpeg 退出码 {return_code}")
                except BaseException:
                    if process.stdin and not process.stdin.closed:
                        process.stdin.close()
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)
                    raise
                partial_path.replace(final_path)
                completed_outputs.append(final_path)
                job.progress = (camera_number + 1) / len(CAMERA_IDS)

            metadata = {
                "date": subject.date,
                "subject_id": subject.subject_id,
                "duration_sec": output_frame_count / preview_fps,
                "fps": preview_fps,
                "frame_count": output_frame_count,
                "timeline_start": subject.timeline_start,
                "timeline_end": subject.timeline_start + output_frame_count / preview_fps,
            }
            metadata_partial = cache_dir / ".metadata.json.part"
            metadata_partial.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            metadata_partial.replace(cache_dir / "metadata.json")
        except BaseException:
            for output_path in completed_outputs:
                output_path.unlink(missing_ok=True)
            for camera_id in CAMERA_IDS:
                (cache_dir / f".{PREVIEW_FILENAMES[camera_id]}.part.mp4").unlink(missing_ok=True)
            raise

    async def run_preview_job(subject: SubjectFrames, cache_dir: Path, job: PreviewJob) -> None:
        try:
            await asyncio.to_thread(generate_preview_sync, subject, cache_dir, job)
        except InterruptedError:
            job.status = "absent"
            job.progress = 0.0
            job.message = "预览视频生成已取消"
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            job.message = "预览视频生成失败"
        else:
            job.status = "ready"
            job.progress = 1.0
            job.camera_id = None
            job.message = "流畅预览视频已生成"

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "ok": resolved_data_root.is_dir(),
            "data_root": str(resolved_data_root),
            "cache_root": str(resolved_cache_root),
            "ffmpeg_available": ffmpeg_binary is not None,
            "part_sets_loaded": len(part_sets),
        }

    @app.get("/api/subjects")
    async def list_subjects() -> dict[str, object]:
        subjects: list[dict[str, object]] = []
        if resolved_data_root.is_dir():
            for date_dir in sorted(resolved_data_root.iterdir(), reverse=True):
                if not date_dir.is_dir() or date_dir.name.startswith("."):
                    continue
                for trial_path in sorted(date_dir.iterdir()):
                    if not trial_path.is_dir() or trial_path.name.startswith("."):
                        continue
                    available = [
                        camera_id
                        for camera_id in CAMERA_IDS
                        if (trial_path / "timestamps" / f"{camera_id}_rgbd_timestamps.csv").is_file()
                    ]
                    if available:
                        preview = preview_status_payload(date_dir.name, trial_path.name)
                        subjects.append(
                            {
                                "date": date_dir.name,
                                "subject_id": trial_path.name,
                                "key": f"{date_dir.name}/{trial_path.name}",
                                "camera_count": len(available),
                                "ready": len(available) == len(CAMERA_IDS),
                                "preview": {
                                    "status": preview["status"],
                                    "progress": preview["progress"],
                                    "cache_bytes": preview["cache_bytes"],
                                    "ffmpeg_available": preview["ffmpeg_available"],
                                },
                            }
                        )
        return {"data_root": str(resolved_data_root), "subjects": subjects}

    @app.get("/api/subjects/{date}/{subject_id}")
    async def subject_metadata(date: str, subject_id: str) -> dict[str, object]:
        subject = load_subject(date, subject_id)
        return {
            "date": subject.date,
            "subject_id": subject.subject_id,
            "duration_sec": subject.duration_sec,
            "timeline_start": subject.timeline_start,
            "timeline_end": subject.timeline_end,
            "cameras": [
                {
                    "camera_id": camera_id,
                    "label": f"RGB 相机 {index}",
                    "frame_count": len(subject.cameras[camera_id]),
                    "width": subject.cameras[camera_id][0].width,
                    "height": subject.cameras[camera_id][0].height,
                }
                for index, camera_id in enumerate(CAMERA_IDS, start=1)
            ],
        }

    @app.get("/api/subjects/{date}/{subject_id}/frame/{camera_id}")
    async def frame_preview(
        date: str,
        subject_id: str,
        camera_id: str,
        position: int = Query(0, ge=0, le=10_000),
    ) -> Response:
        if camera_id not in CAMERA_IDS:
            raise HTTPException(status_code=404, detail="未知相机")
        subject = load_subject(date, subject_id)
        target_timestamp = _position_timestamp(subject, position)
        record = subject.cameras[camera_id][_nearest_record_index(subject, camera_id, target_timestamp)]

        if not record.path.is_file():
            raise HTTPException(status_code=404, detail=f"RGB 帧文件不存在：{record.path.name}")
        raw = np.fromfile(record.path, dtype=np.uint8)
        expected_size = record.width * record.height * 3
        if raw.size != expected_size:
            raise HTTPException(
                status_code=422,
                detail=f"RGB 帧大小异常：期望 {expected_size} 字节，实际 {raw.size} 字节",
            )
        rgb = raw.reshape((record.height, record.width, 3))
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        encoded, jpeg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 86])
        if not encoded:
            raise HTTPException(status_code=500, detail="RGB 帧编码失败")
        return Response(
            content=jpeg.tobytes(),
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=3600",
                "X-Frame-Index": str(record.frame_index),
                "X-Frame-Timestamp": f"{record.timestamp:.6f}",
            },
        )

    @app.get("/api/subjects/{date}/{subject_id}/preview/status")
    async def preview_status(date: str, subject_id: str) -> dict[str, object]:
        subject_path(date, subject_id)
        return preview_status_payload(date, subject_id)

    @app.post("/api/subjects/{date}/{subject_id}/preview", status_code=202)
    async def create_preview(date: str, subject_id: str) -> dict[str, object]:
        subject = load_subject(date, subject_id)
        current = jobs.get((date, subject_id))
        if current and current.status == "generating":
            return preview_status_payload(date, subject_id)
        if preview_status_payload(date, subject_id)["status"] == "ready":
            return preview_status_payload(date, subject_id)
        if ffmpeg_binary is None:
            raise HTTPException(
                status_code=503,
                detail="没有找到 FFmpeg；请使用 scoring_platform/run.sh 从 env_robfit 环境启动",
            )
        job = PreviewJob(cancel_event=threading.Event())
        jobs[(date, subject_id)] = job
        job.task = asyncio.create_task(
            run_preview_job(subject, subject_cache_path(date, subject_id), job)
        )
        return preview_status_payload(date, subject_id)

    @app.get("/api/subjects/{date}/{subject_id}/preview/{camera_id}.mp4")
    async def preview_video(date: str, subject_id: str, camera_id: str) -> FileResponse:
        subject_path(date, subject_id)
        if camera_id not in CAMERA_IDS:
            raise HTTPException(status_code=404, detail="未知相机")
        video_path = subject_cache_path(date, subject_id) / PREVIEW_FILENAMES[camera_id]
        if not video_path.is_file():
            raise HTTPException(status_code=404, detail="预览视频尚未生成")
        return FileResponse(
            video_path,
            media_type="video/mp4",
            filename=PREVIEW_FILENAMES[camera_id],
            content_disposition_type="inline",
            headers={"Cache-Control": "no-cache"},
        )

    @app.delete("/api/subjects/{date}/{subject_id}/preview")
    async def delete_preview(date: str, subject_id: str) -> dict[str, object]:
        subject_path(date, subject_id)
        key = (date, subject_id)
        job = jobs.get(key)
        if job and job.status == "generating" and job.cancel_event:
            job.cancel_event.set()
            if job.task:
                await job.task
        cache_dir = subject_cache_path(date, subject_id)
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir)
        jobs.pop(key, None)
        return preview_status_payload(date, subject_id)

    def _read_annotations() -> dict[str, object]:
        path = annotation_file()
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        subjects = payload.get("subjects") if isinstance(payload, dict) else None
        return subjects if isinstance(subjects, dict) else {}

    def _read_sets(date: str, subject_id: str) -> list[dict[str, object]]:
        entry = _read_annotations().get(_subject_annotation_key(date, subject_id))
        if not isinstance(entry, dict):
            return []
        stored = entry.get("sets")
        if not isinstance(stored, list):
            return []
        sets = [item for item in stored if isinstance(item, dict)]

        def sort_key(item: dict[str, object]) -> object:
            return item.get("id", 0) if isinstance(item.get("id"), int) else 0

        # 按 id 升序:ids 1..11 即固定动作序列顺序(warm_up1-3 + 表格顺序),
        # upsert 与清除都不改变 id,顺序恒稳定
        sets.sort(key=sort_key)
        return sets

    def _persist_annotations(subjects: dict[str, object]) -> None:
        resolved_annotations_root.mkdir(parents=True, exist_ok=True)
        partial_path = resolved_annotations_root / f".{SETS_FILENAME}.part"
        payload = {"schema_version": SETS_SCHEMA_VERSION, "subjects": subjects}
        partial_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        partial_path.replace(annotation_file())

    def _write_sets(date: str, subject_id: str, sets: list[dict[str, object]]) -> None:
        # 调用方必须持有 sets_lock;内部重读整文件,保留其他被试的标注
        subjects = _read_annotations()
        subjects[_subject_annotation_key(date, subject_id)] = {
            "date": date,
            "subject_id": subject_id,
            "sets": sets,
        }
        _persist_annotations(subjects)

    def _seed_records(sequence: list[str]) -> list[dict[str, object]]:
        # 固定序列实体化为 11 张空卡片:ids 1..11 = warm_up1-3 + 表格 8 动作顺序
        labels = list(WARMUP_LABELS) + sequence
        now = datetime.now(timezone.utc).isoformat()
        return [
            {
                "id": index + 1,
                "label": label,
                "start_position": None,
                "end_position": None,
                "start_sec": None,
                "end_sec": None,
                "duration_sec": None,
                "score": None,
                "cameras": {},
                "created_at": now,
                "updated_at": now,
            }
            for index, label in enumerate(labels)
        ]

    def _ensure_subject_seeded(date: str, subject_id: str) -> None:
        # 调用方必须持有 sets_lock。兜底惰性播种:启动后新出现在 data 目录的被试
        # 首次访问时补录 11 张空卡片;表格中不存在则不播种。
        key = _subject_annotation_key(date, subject_id)
        if key in _read_annotations():
            return
        sequence = part_sets.get(subject_id)
        if not sequence:
            return
        _write_sets(date, subject_id, _seed_records(sequence))

    def _seed_all_known_subjects() -> None:
        # 启动时批量预录入:扫描 data 目录,把表格中存在的被试一次性写入 JSON。
        # 幂等:已存在的条目绝不覆盖(保护已有标注);不写文件当且仅当无需新增。
        if not part_sets or not resolved_data_root.is_dir():
            return
        with sets_lock:
            subjects = _read_annotations()
            changed = False
            for date_dir in sorted(resolved_data_root.iterdir()):
                if not date_dir.is_dir() or date_dir.name.startswith("."):
                    continue
                for trial_dir in sorted(date_dir.iterdir()):
                    if not trial_dir.is_dir() or trial_dir.name.startswith("."):
                        continue
                    sequence = part_sets.get(trial_dir.name)
                    if not sequence:
                        continue
                    key = f"{date_dir.name}/{trial_dir.name}"
                    if key in subjects:
                        continue
                    subjects[key] = {
                        "date": date_dir.name,
                        "subject_id": trial_dir.name,
                        "sets": _seed_records(sequence),
                    }
                    changed = True
            if changed:
                _persist_annotations(subjects)

    def _validate_position(value: object, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(status_code=400, detail=f"无效的{label}")
        if value < 0 or value > 10_000:
            raise HTTPException(status_code=400, detail="位置超出有效范围")
        return value

    def _validate_score(value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > SETS_MAX_SCORE:
            raise HTTPException(status_code=400, detail=f"评分需为 0 到 {SETS_MAX_SCORE} 的整数")
        return value

    def _is_warmup_label(label: object) -> bool:
        return label in WARMUP_LABELS

    def _build_set(
        subject: SubjectFrames,
        label: str,
        start_position: int,
        end_position: int,
        score: int | None,
        set_id: int,
        now: str,
    ) -> dict[str, object]:
        start_timestamp = _position_timestamp(subject, start_position)
        end_timestamp = _position_timestamp(subject, end_position)
        boundaries: dict[str, dict[str, dict[str, object]]] = {}
        for camera_id in CAMERA_IDS:
            start_record = subject.cameras[camera_id][
                _nearest_record_index(subject, camera_id, start_timestamp)
            ]
            end_record = subject.cameras[camera_id][
                _nearest_record_index(subject, camera_id, end_timestamp)
            ]
            boundaries[camera_id] = {
                "start": {
                    "frame_index": start_record.frame_index,
                    "timestamp": round(start_record.timestamp, 6),
                },
                "end": {
                    "frame_index": end_record.frame_index,
                    "timestamp": round(end_record.timestamp, 6),
                },
            }
        # 诊断信息:该边界处两相机时间戳的偏移(cam1 - cam2),微秒
        for side in ("start", "end"):
            cam1_timestamp = boundaries["cam1"][side]["timestamp"]
            cam2_timestamp = boundaries["cam2"][side]["timestamp"]
            offset_us = round((cam1_timestamp - cam2_timestamp) * 1_000_000)
            boundaries["cam1"][side]["inter_camera_offset_us"] = offset_us
            boundaries["cam2"][side]["inter_camera_offset_us"] = -offset_us
        return {
            "id": set_id,
            "label": label,
            "start_position": start_position,
            "end_position": end_position,
            "start_sec": round(start_timestamp, 6),
            "end_sec": round(end_timestamp, 6),
            "duration_sec": round(end_timestamp - start_timestamp, 6),
            "score": score,
            "cameras": boundaries,
            "created_at": now,
            "updated_at": now,
        }

    async def _read_request_body(request: Request) -> dict[str, object]:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="请求体无效")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体无效")
        return body

    @app.get("/api/subjects/{date}/{subject_id}/sets")
    async def list_sets(date: str, subject_id: str) -> dict[str, object]:
        subject_path(date, subject_id)
        with sets_lock:
            _ensure_subject_seeded(date, subject_id)
            sets = _read_sets(date, subject_id)
        return {"schema_version": SETS_SCHEMA_VERSION, "sets": sets}

    @app.post("/api/subjects/{date}/{subject_id}/sets", status_code=201)
    async def create_set(date: str, subject_id: str, request: Request, response: Response) -> dict[str, object]:
        subject = load_subject(date, subject_id)
        body = await _read_request_body(request)
        label = body.get("label")
        if not isinstance(label, str) or not label or len(label) > 64:
            raise HTTPException(status_code=400, detail="无效的动作标签")
        expected_labels = list(WARMUP_LABELS) + part_sets.get(subject_id, [])
        if label not in expected_labels:
            raise HTTPException(status_code=400, detail="该动作标签不属于此被试的固定序列")
        start_position = _validate_position(body.get("start_position"), "起始位置")
        end_position = _validate_position(body.get("end_position"), "结束位置")
        if start_position >= end_position:
            raise HTTPException(status_code=400, detail="结束位置必须大于起始位置")
        if label in WARMUP_LABELS:
            if body.get("score") is not None:
                raise HTTPException(status_code=400, detail="热身动作无需评分")
            score = None
        else:
            score = _validate_score(body.get("score"))
        now = datetime.now(timezone.utc).isoformat()
        with sets_lock:
            _ensure_subject_seeded(date, subject_id)
            sets = _read_sets(date, subject_id)
            target = next((item for item in sets if item.get("label") == label), None)
            if target is not None:
                # upsert:卡片已存在则原位更新,保留 id 与 created_at
                record = _build_set(subject, label, start_position, end_position, score, target["id"], now)
                record["created_at"] = target.get("created_at") or now
                sets[sets.index(target)] = record
                response.status_code = 200
            else:
                # 防御:序列中的卡片缺失(如手改文件),补建
                set_id = max((item["id"] for item in sets if isinstance(item.get("id"), int)), default=0) + 1
                record = _build_set(subject, label, start_position, end_position, score, set_id, now)
                sets.append(record)
            _write_sets(date, subject_id, sets)
        return {"set": record, "count": len(sets)}

    @app.patch("/api/subjects/{date}/{subject_id}/sets/{set_id:int}")
    async def update_set_score(date: str, subject_id: str, set_id: int, request: Request) -> dict[str, object]:
        subject_path(date, subject_id)
        body = await _read_request_body(request)
        if "score" not in body:
            raise HTTPException(status_code=400, detail="缺少评分字段")
        score = _validate_score(body["score"])
        now = datetime.now(timezone.utc).isoformat()
        with sets_lock:
            _ensure_subject_seeded(date, subject_id)
            sets = _read_sets(date, subject_id)
            target = next((item for item in sets if item.get("id") == set_id), None)
            if target is None:
                raise HTTPException(status_code=404, detail="没有找到该动作记录")
            if _is_warmup_label(target.get("label")):
                raise HTTPException(status_code=400, detail="热身动作无需评分")
            target["score"] = score
            target["updated_at"] = now
            _write_sets(date, subject_id, sets)
        return {"set": target, "count": len(sets)}

    @app.delete("/api/subjects/{date}/{subject_id}/sets/{set_id:int}")
    async def delete_set(date: str, subject_id: str, set_id: int) -> dict[str, object]:
        subject_path(date, subject_id)
        now = datetime.now(timezone.utc).isoformat()
        with sets_lock:
            _ensure_subject_seeded(date, subject_id)
            sets = _read_sets(date, subject_id)
            target = next((item for item in sets if item.get("id") == set_id), None)
            if target is None:
                raise HTTPException(status_code=404, detail="没有找到该动作记录")
            # 清除语义:保留卡片与 id/label,只清空录入的分段与分数
            target.update({
                "start_position": None,
                "end_position": None,
                "start_sec": None,
                "end_sec": None,
                "duration_sec": None,
                "score": None,
                "cameras": {},
                "updated_at": now,
            })
            _write_sets(date, subject_id, sets)
        return {"set": target, "count": len(sets)}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_ROOT / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/assets", NoCacheStaticFiles(directory=FRONTEND_ROOT), name="assets")
    _seed_all_known_subjects()
    return app


app = create_app()
