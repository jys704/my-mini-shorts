from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

APP = FastAPI(title="AI Shorts Mini MVP", version="0.2.0")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "mini_shorts_data"
DB_PATH = DATA_DIR / "app.db"
STORAGE_DIR = DATA_DIR / "storage"

DATA_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            language TEXT NOT NULL,
            target_duration INTEGER NOT NULL,
            status TEXT NOT NULL,
            current_step TEXT NOT NULL,
            script_text TEXT,
            scene_plan TEXT,
            asset_manifest TEXT,
            youtube_url TEXT,
            youtube_video_id TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


class JobCreate(BaseModel):
    topic: str = Field(..., min_length=2, max_length=255)
    channel_name: str = Field(default="Test Channel", min_length=2, max_length=120)
    language: str = Field(default="ko", max_length=10)
    target_duration: int = Field(default=45, ge=15, le=60)


class YouTubeMarkUploaded(BaseModel):
    video_id: str = Field(..., min_length=3)
    youtube_url: str = Field(..., min_length=10)


class JobResponse(BaseModel):
    id: str
    topic: str
    channel_name: str
    language: str
    target_duration: int
    status: str
    current_step: str
    script_text: str | None = None
    scene_plan: dict | None = None
    asset_manifest: dict | None = None
    youtube_video_id: str | None = None
    youtube_url: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str



def now_iso() -> str:
    return datetime.utcnow().isoformat()



def build_script(topic: str, seconds: int) -> str:
    return "\n".join(
        [
            f"[훅] {topic}에서 사람들이 가장 놀라는 포인트 1개",
            f"[전개] 핵심 내용 3개를 {seconds}초 안에 압축 설명",
            "[마무리] 마지막 3초에 반전 또는 행동 유도 한 줄",
        ]
    )



def split_scenes(script_text: str) -> dict:
    lines = [line.strip() for line in script_text.splitlines() if line.strip()]
    scenes = []
    for i, line in enumerate(lines, start=1):
        scenes.append(
            {
                "scene_number": i,
                "voice_line": line,
                "visual_prompt": f"Vertical short cinematic scene: {line}",
                "duration_seconds": 5 if i < len(lines) else 3,
            }
        )
    return {"scenes": scenes}



def build_assets(job_id: str, scene_plan: dict) -> dict:
    root = STORAGE_DIR / job_id
    video_dir = root / "video"
    audio_dir = root / "audio"
    subtitle_dir = root / "subtitle"
    for p in [video_dir, audio_dir, subtitle_dir]:
        p.mkdir(parents=True, exist_ok=True)

    for scene in scene_plan["scenes"]:
        (video_dir / f"scene_{scene['scene_number']}.txt").write_text(scene["visual_prompt"], encoding="utf-8")
        (audio_dir / f"scene_{scene['scene_number']}.txt").write_text(scene["voice_line"], encoding="utf-8")

    subtitle_path = subtitle_dir / "captions.vtt"
    subtitle_path.write_text("WEBVTT\n\n00:00.000 --> 00:03.000\n자동 생성 자막 샘플\n", encoding="utf-8")
    thumbnail_path = root / "thumbnail.txt"
    thumbnail_path.write_text("썸네일 문구 샘플", encoding="utf-8")
    final_video_path = root / "final_short.mp4"

    manifest = {
        "root": str(root),
        "video_assets": [str(p) for p in sorted(video_dir.glob("*"))],
        "audio_assets": [str(p) for p in sorted(audio_dir.glob("*"))],
        "subtitle": str(subtitle_path),
        "thumbnail": str(thumbnail_path),
        "final_video": str(final_video_path),
        "final_video_ready": final_video_path.exists(),
        "youtube": {"status": "pending", "video_id": None, "url": None},
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest



def render_test_video(job_id: str, duration: int) -> str:
    root = STORAGE_DIR / job_id
    output_path = root / "final_short.mp4"

    command = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s=1080x1920:d={duration}",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=stereo",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(output_path),
    ]

    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return str(output_path)



def row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "topic": row["topic"],
        "channel_name": row["channel_name"],
        "language": row["language"],
        "target_duration": row["target_duration"],
        "status": row["status"],
        "current_step": row["current_step"],
        "script_text": row["script_text"],
        "scene_plan": json.loads(row["scene_plan"]) if row["scene_plan"] else None,
        "asset_manifest": json.loads(row["asset_manifest"]) if row["asset_manifest"] else None,
        "youtube_video_id": row["youtube_video_id"],
        "youtube_url": row["youtube_url"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@APP.get("/health")
def health():
    return {"status": "ok"}


@APP.post("/jobs", response_model=JobResponse)
def create_job(payload: JobCreate):
    job_id = str(uuid.uuid4())
    created_at = now_iso()

    try:
        script_text = build_script(payload.topic, payload.target_duration)
        scene_plan = split_scenes(script_text)
        asset_manifest = build_assets(job_id, scene_plan)
        final_video_path = render_test_video(job_id, payload.target_duration)

        asset_manifest["final_video"] = final_video_path
        asset_manifest["final_video_ready"] = True
        asset_manifest["video_render_mode"] = "ffmpeg_test_mp4"
        (Path(asset_manifest["root"]) / "manifest.json").write_text(
            json.dumps(asset_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        conn = db()
        conn.execute(
            """
            INSERT INTO jobs (
                id, topic, channel_name, language, target_duration, status, current_step,
                script_text, scene_plan, asset_manifest, youtube_url, youtube_video_id,
                error_message, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                payload.topic,
                payload.channel_name,
                payload.language,
                payload.target_duration,
                "completed",
                "video_rendered",
                script_text,
                json.dumps(scene_plan, ensure_ascii=False),
                json.dumps(asset_manifest, ensure_ascii=False),
                None,
                None,
                None,
                created_at,
                created_at,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        conn.close()
        return row_to_dict(row)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@APP.get("/jobs", response_model=List[JobResponse])
def list_jobs():
    conn = db()
    rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    conn.close()
    return [row_to_dict(row) for row in rows]


@APP.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str):
    conn = db()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="job_not_found")
    return row_to_dict(row)


@APP.post("/jobs/{job_id}/mark-uploaded", response_model=JobResponse)
def mark_uploaded(job_id: str, payload: YouTubeMarkUploaded):
    conn = db()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="job_not_found")

    asset_manifest = json.loads(row["asset_manifest"]) if row["asset_manifest"] else {}
    asset_manifest["youtube"] = {
        "status": "uploaded",
        "video_id": payload.video_id,
        "url": payload.youtube_url,
    }

    updated_at = now_iso()
    conn.execute(
        """
        UPDATE jobs
        SET youtube_video_id = ?, youtube_url = ?, asset_manifest = ?, current_step = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            payload.video_id,
            payload.youtube_url,
            json.dumps(asset_manifest, ensure_ascii=False),
            "youtube_uploaded",
            updated_at,
            job_id,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return row_to_dict(row)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("mini_shorts_mvp_ffmpeg:APP", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
