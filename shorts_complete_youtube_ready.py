"""
AI Shorts Automation - Complete MVP
Run: python shorts_complete_youtube_ready.py
Dashboard: http://localhost:8000
API Docs: http://localhost:8000/docs
"""
import os, json, uuid, sqlite3, subprocess, textwrap, threading, platform
from datetime import datetime
from pathlib import Path
from typing import Optional, List


def load_env_file(env_path: str = ".env"):
    path = Path(env_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

# ─── Config ───────────────────────────────────────────────────────────────────
APP_ROOT   = Path(__file__).resolve().parent
BASE_DIR   = APP_ROOT / "shorts_data"
DB_PATH    = BASE_DIR / "app.db"
STORAGE    = BASE_DIR / "storage"
ASSETS_DIR = BASE_DIR / "assets"
VIDEO_ASSET_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
IMAGE_ASSET_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_ASSET_EXTS = VIDEO_ASSET_EXTS | IMAGE_ASSET_EXTS
DEFAULT_TRANSITION = "fade"
TRANSITION_DURATION = 0.4
SUPPORTED_TRANSITIONS = {"fade", "wipeleft", "wiperight", "slideleft", "slideright"}
BASE_DIR.mkdir(exist_ok=True)
STORAGE.mkdir(exist_ok=True)
ASSETS_DIR.mkdir(exist_ok=True)

# API keys (set in env or .env file)
OPENAI_KEY      = os.getenv("OPENAI_API_KEY", "")
ELEVENLABS_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE= os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
YT_CLIENT_ID    = os.getenv("YOUTUBE_CLIENT_ID", "")
YT_CLIENT_SECRET= os.getenv("YOUTUBE_CLIENT_SECRET", "")
YT_REFRESH_TOKEN= os.getenv("YOUTUBE_REFRESH_TOKEN", "")

# ─── DB ───────────────────────────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            language TEXT DEFAULT 'ko',
            target_duration INTEGER DEFAULT 45,
            status TEXT DEFAULT 'queued',
            current_step TEXT DEFAULT 'queued',
            script_text TEXT,
            scene_plan TEXT,
            asset_manifest TEXT,
            youtube_video_id TEXT,
            youtube_url TEXT,
            error_message TEXT,
            created_at TEXT,
            updated_at TEXT
        )""")
        c.commit()

init_db()

# ─── Models ───────────────────────────────────────────────────────────────────
class JobCreate(BaseModel):
    topic: str
    channel_name: str
    language: str = "ko"
    target_duration: int = 45

class JobResponse(BaseModel):
    id: str
    topic: str
    channel_name: str
    language: str
    target_duration: int
    status: str
    current_step: str
    script_text: Optional[str] = None
    scene_plan: Optional[str] = None
    asset_manifest: Optional[str] = None
    youtube_video_id: Optional[str] = None
    youtube_url: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str
    updated_at: str

class MarkUploaded(BaseModel):
    video_id: str
    youtube_url: str

# ─── DB Helpers ───────────────────────────────────────────────────────────────
def db_get(job_id: str):
    with get_conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None

def db_list(limit=50):
    with get_conn() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

def db_update(job_id: str, **kw):
    kw["updated_at"] = datetime.utcnow().isoformat()
    sets = ", ".join(f"{k}=?" for k in kw)
    vals = list(kw.values()) + [job_id]
    with get_conn() as c:
        c.execute(f"UPDATE jobs SET {sets} WHERE id=?", vals)
        c.commit()

# ─── Script Generation ────────────────────────────────────────────────────────
def build_script(topic: str, duration: int, lang: str) -> str:
    if OPENAI_KEY:
        try:
            import httpx
            prompt = f"""당신은 유튜브 숏츠 전문 작가입니다.
주제: {topic}
언어: {'한국어' if lang=='ko' else 'English'}
길이: {duration}초 (약 {duration*3}자 내외)

규칙:
- 첫 문장은 강렬한 훅 (시청자 시선 잡기)
- 감정적이고 짧은 문장
- 총 5~7문장
- 마지막은 행동 유도 (좋아요/구독)

스크립트만 출력하세요."""
            r = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                json={"model": "gpt-4o-mini", "messages": [{"role":"user","content":prompt}], "max_tokens":300},
                timeout=15
            )
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            pass  # fallback below

    # 플레이스홀더 스크립트
    hooks = {
        "ko": f"충격적인 사실! {topic}에 대해 아무도 알려주지 않는 비밀이 있습니다.",
        "en": f"Shocking truth about {topic} nobody tells you!"
    }
    lines = [
        hooks.get(lang, hooks["ko"]),
        f"{topic}은(는) 우리 삶을 완전히 바꿀 수 있습니다.",
        "전문가들도 이 사실에 놀랐습니다.",
        "지금부터 핵심 3가지를 알려드릴게요.",
        f"첫째, {topic}의 가장 중요한 특징입니다.",
        "이것을 알면 당신의 관점이 달라집니다.",
        "좋아요와 구독으로 더 많은 정보를 받아보세요!"
    ]
    return "\n".join(lines)

# ─── Scene Planner ────────────────────────────────────────────────────────────
def split_script_lines(script: str) -> list:
    lines = [l.strip() for l in script.split("\n") if l.strip()]
    if len(lines) <= 1:
        sentence_marks = [". ", "! ", "? ", "。", "！", "？"]
        normalized = script.strip()
        for mark in sentence_marks:
            normalized = normalized.replace(mark, mark.strip() + "\n")
        lines = [l.strip() for l in normalized.split("\n") if l.strip()]
    return lines or ["테스트 영상입니다."]


def extract_scene_keywords(text: str, limit: int = 5) -> list:
    stopwords = {
        "그리고", "하지만", "그러나", "입니다", "합니다", "있습니다",
        "about", "with", "that", "this", "your", "from", "the", "and"
    }
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    keywords = []
    for word in cleaned.split():
        normalized = word.strip()
        if len(normalized) < 2 or normalized.lower() in stopwords:
            continue
        if normalized not in keywords:
            keywords.append(normalized)
        if len(keywords) >= limit:
            break
    return keywords


def split_scenes(script: str, duration: int) -> list:
    lines = split_script_lines(script)

    # 장면 수가 너무 많으면 목표 길이를 초과하므로 축소
    max_scenes = max(1, min(len(lines), duration))
    lines = lines[:max_scenes]

    scenes = []
    base = duration // len(lines)
    remainder = duration % len(lines)
    cursor = 0

    for i, line in enumerate(lines):
        scene_duration = max(1, base + (1 if i < remainder else 0))
        start = cursor
        end = start + scene_duration
        scenes.append({
            "scene": i + 1,
            "text": line,
            "start": start,
            "end": end,
            "keywords": extract_scene_keywords(line),
            "asset_path": None,
            "transition": "none" if i == 0 else DEFAULT_TRANSITION,
            # 기존 렌더링 호환 필드: build_video에서 계속 사용
            "voice_line": line,
            "visual_prompt": f"신비롭고 역동적인 배경, 텍스트: '{line[:20]}...'",
            "duration": scene_duration
        })
        cursor = end
    return scenes


def scene_plan_to_json(scenes: list) -> str:
    return json.dumps(scenes, ensure_ascii=False, indent=2)


def save_scene_plan(job_id: str, scenes: list) -> Optional[Path]:
    job_dir = STORAGE / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    scene_plan_path = job_dir / "scene_plan.json"
    scene_plan_path.write_text(scene_plan_to_json(scenes), encoding="utf-8")
    return scene_plan_path


def persist_scene_plan(job_id: str, scenes: list, current_step: Optional[str] = None) -> Optional[Path]:
    scene_plan_path = None
    try:
        scene_plan_path = save_scene_plan(job_id, scenes)
    except Exception as e:
        print(f"scene_plan.json 저장 실패, DB에는 scene_plan 유지: {e}")

    update_data = {"scene_plan": scene_plan_to_json(scenes)}
    if current_step:
        update_data["current_step"] = current_step
    db_update(job_id, **update_data)
    return scene_plan_path


def find_asset_files(asset_dir: Path = ASSETS_DIR) -> list:
    asset_dir = Path(asset_dir)
    asset_dir.mkdir(parents=True, exist_ok=True)
    return sorted(
        path.resolve() for path in asset_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_ASSET_EXTS
    )


def score_asset_for_scene(asset_path: Path, scene: dict) -> int:
    searchable = f"{asset_path.stem} {asset_path.parent.name}".lower()
    score = 0
    for keyword in scene.get("keywords") or []:
        normalized = str(keyword).lower()
        if normalized and normalized in searchable:
            score += 2
    text = str(scene.get("text") or scene.get("voice_line") or "").lower()
    for token in searchable.replace("_", " ").replace("-", " ").split():
        if len(token) >= 2 and token in text:
            score += 1
    return score


def select_assets_for_scenes(scenes: list, asset_dir: Path = ASSETS_DIR, assets: Optional[list] = None) -> list:
    asset_dir = Path(asset_dir)
    assets = find_asset_files(asset_dir) if assets is None else list(assets)
    asset_count = len(assets)
    if not assets:
        for scene in scenes:
            scene["asset_path"] = None
            scene["asset_score"] = 0
            scene["asset_status"] = "no_assets_found"
        return scenes

    for index, scene in enumerate(scenes):
        scored_assets = [
            (score_asset_for_scene(asset, scene), asset_index, asset)
            for asset_index, asset in enumerate(assets)
        ]
        best_score, _, best_asset = max(scored_assets, key=lambda item: (item[0], -item[1]))
        if best_score == 0:
            best_asset = assets[index % asset_count]
        scene["asset_path"] = str(best_asset)
        scene["asset_score"] = best_score
        scene["asset_status"] = "matched" if best_score > 0 else "round_robin"
    return scenes


def prepare_scene_plan_for_job(job_id: str, script: str, duration: int) -> tuple:
    scenes = split_scenes(script, duration)
    asset_files = []
    try:
        asset_files = find_asset_files(ASSETS_DIR)
        select_assets_for_scenes(scenes, ASSETS_DIR, asset_files)
    except Exception as e:
        print(f"asset 자동 선택 실패, scene_plan.json은 fallback 상태로 저장: {e}")
        for scene in scenes:
            scene.setdefault("asset_path", None)
            scene.setdefault("asset_score", 0)
            scene["asset_status"] = "asset_select_failed"

    scene_plan_path = persist_scene_plan(job_id, scenes, current_step="rendering")
    return scenes, asset_files, scene_plan_path

# ─── TTS (ElevenLabs or silent) ───────────────────────────────────────────────
def synthesize_voice(text: str, out_path: Path, fallback_duration: float = 3.0) -> bool:
    if ELEVENLABS_KEY:
        try:
            import httpx
            r = httpx.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE}",
                headers={"xi-api-key": ELEVENLABS_KEY, "Content-Type": "application/json"},
                json={"text": text, "model_id": "eleven_multilingual_v2",
                      "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}},
                timeout=30
            )
            if r.status_code == 200:
                out_path.write_bytes(r.content)
                return True
        except Exception:
            pass

    # 무음 오디오 생성 (ffmpeg): 실제 scene 길이를 사용해 기존 렌더링 fallback 유지
    safe_duration = max(1.0, float(fallback_duration or 3.0))
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-t", f"{safe_duration:.2f}", str(out_path)
    ], capture_output=True)
    return False


def probe_media_duration(media_path: Path) -> Optional[float]:
    if not media_path.exists():
        return None
    try:
        ret = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(media_path)
        ], capture_output=True, text=True)
        if ret.returncode != 0:
            return None
        duration = float((ret.stdout or "").strip())
        if duration > 0:
            return duration
    except Exception:
        return None
    return None


def sync_scene_duration_from_audio(scene: dict, audio_file: Path, fallback_duration: float) -> float:
    audio_duration = probe_media_duration(audio_file)
    if audio_duration:
        synced_duration = max(1.0, round(audio_duration, 2))
        scene["audio_duration"] = synced_duration
        scene["audio_sync"] = "ffprobe"
    else:
        synced_duration = max(1.0, float(fallback_duration or scene.get("duration") or 1))
        scene["audio_duration"] = None
        scene["audio_sync"] = "fallback"

    scene["duration"] = synced_duration
    scene["end"] = round(float(scene.get("start", 0)) + synced_duration, 2)
    return synced_duration


def recalculate_scene_timing(scenes: list) -> list:
    cursor = 0.0
    for scene in scenes:
        duration = max(1.0, float(scene.get("duration") or 1))
        scene["start"] = round(cursor, 2)
        cursor += duration
        scene["end"] = round(cursor, 2)
    return scenes

# ─── FFmpeg Video Builder ─────────────────────────────────────────────────────
def get_korean_font_path() -> str:
    candidates = []
    if platform.system() == "Windows":
        candidates = [
            r"C:\Windows\Fonts\malgun.ttf",
            r"C:\Windows\Fonts\malgunsl.ttf",
            r"C:\Windows\Fonts\gulim.ttc",
            r"C:\Windows\Fonts\batang.ttc",
            r"C:\Windows\Fonts\arial.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]

    for path in candidates:
        if os.path.exists(path):
            return path
    return ""


def normalize_transition(value: str) -> Optional[str]:
    transition = str(value or "none").strip().lower()
    if transition in SUPPORTED_TRANSITIONS:
        return transition
    return None


def render_broll_scene(asset_path: str, audio_file: Path, scene_vid: Path, dur: int, drawtext_filter: str) -> bool:
    asset = Path(asset_path) if asset_path else None
    if not asset or not asset.exists() or asset.suffix.lower() not in SUPPORTED_ASSET_EXTS:
        return False

    media_filter = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1,"
        f"{drawtext_filter}[v]"
    )
    input_args = ["-stream_loop", "-1", "-i", str(asset)]
    if asset.suffix.lower() in IMAGE_ASSET_EXTS:
        input_args = ["-loop", "1", "-i", str(asset)]

    ret = subprocess.run([
        "ffmpeg", "-y",
        *input_args,
        "-i", str(audio_file),
        "-filter_complex", media_filter,
        "-map", "[v]", "-map", "1:a",
        "-t", str(dur),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest",
        str(scene_vid)
    ], capture_output=True, text=True)

    if ret.returncode != 0 and scene_vid.exists():
        scene_vid.unlink(missing_ok=True)
    return ret.returncode == 0 and scene_vid.exists()


def build_transition_video(job_dir: Path, scene_files: list, scenes: list) -> Optional[Path]:
    if len(scene_files) < 2:
        return None

    transitions = [normalize_transition(scene.get("transition")) for scene in scenes[1:len(scene_files)]]
    if not any(transitions):
        return None

    filter_parts = []
    for i in range(len(scene_files)):
        filter_parts.append(
            f"[{i}:v]fps=30,scale=1080:1920,format=yuv420p,setpts=PTS-STARTPTS[v{i}]"
        )
        filter_parts.append(f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]")

    previous_video = "v0"
    current_end = float(scenes[0].get("duration") or 1)
    rendered_transition_count = 0
    for i in range(1, len(scene_files)):
        transition = transitions[i - 1]
        scene_duration = float(scenes[i].get("duration") or 1)
        if not transition:
            return None
        transition_duration = min(TRANSITION_DURATION, current_end / 2, scene_duration / 2)
        offset = max(0, current_end - transition_duration)
        output_label = f"vx{i}"
        filter_parts.append(
            f"[{previous_video}][v{i}]xfade=transition={transition}:"
            f"duration={transition_duration:.2f}:offset={offset:.2f}[{output_label}]"
        )
        previous_video = output_label
        current_end = current_end + scene_duration - transition_duration
        rendered_transition_count += 1

    audio_inputs = "".join(f"[a{i}]" for i in range(len(scene_files)))
    filter_parts.append(f"{audio_inputs}concat=n={len(scene_files)}:v=0:a=1[aout]")

    final = job_dir / "final_short.mp4"
    input_args = []
    for scene_file in scene_files:
        input_args.extend(["-i", str(scene_file)])

    ret = subprocess.run([
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", ";".join(filter_parts),
        "-map", f"[{previous_video}]", "-map", "[aout]",
        "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p",
        "-shortest",
        str(final)
    ], capture_output=True, text=True)

    if ret.returncode != 0:
        if final.exists():
            final.unlink(missing_ok=True)
        return None

    for scene in scenes[1:1 + rendered_transition_count]:
        scene["transition_rendered"] = True
    return final if final.exists() else None


def build_video(job_id: str, scenes: list, script: str) -> Optional[Path]:
    job_dir = STORAGE / job_id
    video_dir = job_dir / "video"
    audio_dir = job_dir / "audio"
    video_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    scene_files = []
    font_path = get_korean_font_path()

    for scene in scenes:
        idx = scene["scene"]
        fallback_dur = float(scene.get("duration") or 1)
        text = scene["voice_line"]
        # 텍스트 줄바꿈 (25자)
        wrapped = "\n".join(textwrap.wrap(text, 22))
        scene_vid = video_dir / f"scene_{idx:02d}.mp4"
        audio_file = audio_dir / f"scene_{idx:02d}.mp3"

        # TTS 생성 후 실제 오디오 길이 기준으로 scene duration 동기화
        synthesize_voice(text, audio_file, fallback_dur)
        dur = sync_scene_duration_from_audio(scene, audio_file, fallback_dur)

        # FFmpeg: B-roll(asset_path) 배경 + 텍스트 오버레이, 실패 시 기존 컬러 배경 유지
        escaped = wrapped.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace("%", "\\%").replace("\n", "\\n")
        bg_color = ["0x1a1a2e", "0x16213e", "0x0f3460", "0x533483"][idx % 4]
        font_expr = ""
        if font_path:
            ff_font = font_path.replace("\\", "/").replace(":", "\\:")
            font_expr = f"fontfile='{ff_font}':"

        drawtext_filter = (
            f"drawtext={font_expr}text='{escaped}':fontcolor=white:fontsize=52:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=10:"
            f"borderw=3:bordercolor=black@0.8:box=1:boxcolor=black@0.25:boxborderw=20"
        )
        ret = None
        broll_rendered = render_broll_scene(scene.get("asset_path"), audio_file, scene_vid, dur, drawtext_filter)
        scene["broll_rendered"] = broll_rendered

        if not broll_rendered:
            ret = subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"color=c={bg_color}:size=1080x1920:rate=30",
                "-i", str(audio_file),
                "-vf", drawtext_filter,
                "-t", str(dur),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-shortest",
                str(scene_vid)
            ], capture_output=True, text=True)

        if ret is not None and ret.returncode != 0:
            # 오디오 없이 재시도
            subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"color=c={bg_color}:size=1080x1920:rate=30",
                "-vf",
                f"drawtext={font_expr}text='{escaped}':fontcolor=white:fontsize=52:"
                f"x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.25:boxborderw=20",
                "-t", str(dur), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(scene_vid)
            ], capture_output=True)

        if scene_vid.exists():
            scene_files.append(scene_vid)

    if not scene_files:
        return None

    recalculate_scene_timing(scenes)

    transition_final = build_transition_video(job_dir, scene_files, scenes)
    if transition_final:
        return transition_final

    # concat
    concat_list = job_dir / "concat.txt"
    concat_list.write_text("\n".join(f"file '{f.resolve()}'" for f in scene_files))
    final = job_dir / "final_short.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p",
        str(final)
    ], capture_output=True)

    return final if final.exists() else None

# ─── YouTube Upload ───────────────────────────────────────────────────────────
def get_yt_access_token() -> Optional[str]:
    if not (YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN):
        return None
    try:
        import httpx
        r = httpx.post("https://oauth2.googleapis.com/token", data={
            "client_id": YT_CLIENT_ID,
            "client_secret": YT_CLIENT_SECRET,
            "refresh_token": YT_REFRESH_TOKEN,
            "grant_type": "refresh_token"
        }, timeout=10)
        return r.json().get("access_token")
    except Exception:
        return None

def upload_to_youtube(job_id: str, video_path: Path, title: str, description: str) -> dict:
    token = get_yt_access_token()
    if not token:
        return {"error": "YouTube API 키 미설정. .env에 YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN 입력 필요"}

    try:
        import httpx
        metadata = {
            "snippet": {
                "title": title[:100],
                "description": description,
                "tags": ["shorts", "AI", "자동화"],
                "categoryId": "22"
            },
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
        }
        # 1단계: 업로드 세션 시작
        init_r = httpx.post(
            "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(video_path.stat().st_size)
            },
            json=metadata, timeout=30
        )
        upload_url = init_r.headers.get("Location")
        if not upload_url:
            return {"error": "업로드 URL 획득 실패"}

        # 2단계: 파일 업로드
        with open(video_path, "rb") as f:
            up_r = httpx.put(
                upload_url,
                content=f.read(),
                headers={"Content-Type": "video/mp4"},
                timeout=300
            )

        if up_r.status_code in (200, 201):
            data = up_r.json()
            vid_id = data.get("id", "")
            return {
                "video_id": vid_id,
                "youtube_url": f"https://youtube.com/watch?v={vid_id}",
                "status": "uploaded"
            }
        return {"error": f"업로드 실패: HTTP {up_r.status_code}"}
    except Exception as e:
        return {"error": str(e)}

def upload_existing_job_to_youtube(job_id: str) -> dict:
    row = db_get(job_id)
    if not row:
        return {"error": "Job not found"}

    manifest = json.loads(row.get("asset_manifest") or "{}")
    video_path = manifest.get("final_video") or str(STORAGE / job_id / "final_short.mp4")
    if not video_path or not os.path.exists(video_path):
        return {"error": "업로드할 final_short.mp4 파일을 찾을 수 없습니다."}

    result = upload_to_youtube(
        job_id,
        Path(video_path),
        title=f"[Shorts] {row['topic']}",
        description=f"{row.get('script_text') or row['topic']}\n\n#shorts #AI #{row['topic'].replace(' ','')}"
    )
    if "video_id" in result:
        manifest["youtube"] = result
        db_update(
            job_id,
            youtube_video_id=result["video_id"],
            youtube_url=result["youtube_url"],
            asset_manifest=json.dumps(manifest, ensure_ascii=False),
            current_step="uploaded",
            status="completed",
        )
    else:
        manifest.setdefault("youtube", {})
        manifest["youtube"]["status"] = "failed"
        manifest["youtube"]["error"] = result.get("error")
        db_update(job_id, asset_manifest=json.dumps(manifest, ensure_ascii=False))
    return result

# ─── Pipeline ─────────────────────────────────────────────────────────────────
def run_pipeline(job_id: str, topic: str, channel: str, lang: str, duration: int):
    try:
        db_update(job_id, status="running", current_step="scripting")

        script = build_script(topic, duration, lang)
        db_update(job_id, script_text=script, current_step="scene_split")

        db_update(job_id, current_step="asset_select")
        scenes, asset_files, scene_plan_path = prepare_scene_plan_for_job(job_id, script, duration)

        final_vid = build_video(job_id, scenes, script)
        scene_plan_path = persist_scene_plan(job_id, scenes) or scene_plan_path
        selected_assets = [scene.get("asset_path") for scene in scenes if scene.get("asset_path")]
        broll_scene_count = sum(1 for scene in scenes if scene.get("broll_rendered"))
        transition_scene_count = sum(1 for scene in scenes if scene.get("transition_rendered"))
        audio_synced_count = sum(1 for scene in scenes if scene.get("audio_sync") == "ffprobe")
        manifest = {
            "job_id": job_id,
            "final_video": str(final_vid) if final_vid else None,
            "final_video_ready": final_vid is not None and final_vid.exists(),
            "scenes": len(scenes),
            "scene_plan_path": str(scene_plan_path) if scene_plan_path else str(STORAGE / job_id / "scene_plan.json"),
            "assets_dir": str(ASSETS_DIR),
            "asset_scan_count": len(asset_files),
            "asset_scan_files": [str(asset) for asset in asset_files],
            "selected_assets": selected_assets,
            "selected_asset_count": len(selected_assets),
            "broll_scene_count": broll_scene_count,
            "transition_scene_count": transition_scene_count,
            "audio_synced_count": audio_synced_count,
            "total_synced_duration": scenes[-1].get("end") if scenes else 0,
            "youtube": {"status": "pending"}
        }
        db_update(job_id, asset_manifest=json.dumps(manifest, ensure_ascii=False),
                  current_step="video_rendered" if final_vid else "render_failed",
                  status="completed" if final_vid else "failed")

        # YouTube 자동 업로드 (키 설정 시)
        if final_vid and YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN:
            db_update(job_id, current_step="uploading")
            result = upload_to_youtube(
                job_id, final_vid,
                title=f"[Shorts] {topic}",
                description=f"{script}\n\n#shorts #AI #{topic.replace(' ','')}"
            )
            if "video_id" in result:
                manifest["youtube"] = result
                db_update(job_id,
                    youtube_video_id=result["video_id"],
                    youtube_url=result["youtube_url"],
                    asset_manifest=json.dumps(manifest, ensure_ascii=False),
                    current_step="uploaded", status="completed")
            else:
                manifest["youtube"]["error"] = result.get("error")
                db_update(job_id, asset_manifest=json.dumps(manifest, ensure_ascii=False))

    except Exception as e:
        db_update(job_id, status="failed", error_message=str(e), current_step="error")

# ─── FastAPI App ──────────────────────────────────────────────────────────────
app = FastAPI(title="AI Shorts Automation", version="2.0")

@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0"}

@app.post("/jobs", response_model=JobResponse)
def create_job(data: JobCreate, bg: BackgroundTasks):
    job_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    with get_conn() as c:
        c.execute("""
        INSERT INTO jobs (id,topic,channel_name,language,target_duration,status,current_step,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """, (job_id, data.topic, data.channel_name, data.language, data.target_duration,
              "queued", "queued", now, now))
        c.commit()
    bg.add_task(run_pipeline, job_id, data.topic, data.channel_name, data.language, data.target_duration)
    row = db_get(job_id)
    return JobResponse(**row)

@app.get("/jobs", response_model=List[JobResponse])
def list_jobs():
    return [JobResponse(**r) for r in db_list()]

@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str):
    row = db_get(job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    return JobResponse(**row)

@app.post("/jobs/{job_id}/upload-youtube")
def upload_job_to_youtube(job_id: str):
    row = db_get(job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    result = upload_existing_job_to_youtube(job_id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return {"ok": True, **result}

@app.post("/jobs/{job_id}/mark-uploaded")
def mark_uploaded(job_id: str, data: MarkUploaded):
    row = db_get(job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    manifest = json.loads(row["asset_manifest"] or "{}")
    manifest["youtube"] = {"status": "uploaded", "video_id": data.video_id, "url": data.youtube_url}
    db_update(job_id, youtube_video_id=data.video_id, youtube_url=data.youtube_url,
              asset_manifest=json.dumps(manifest, ensure_ascii=False),
              current_step="uploaded", status="completed")
    return {"ok": True, "youtube_url": data.youtube_url}

@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    with get_conn() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        c.commit()
    import shutil
    job_dir = STORAGE / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
    return {"ok": True, "deleted": job_id}

# ─── Dashboard (HTML) ─────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Shorts Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#0f0f1a;color:#e0e0e0;min-height:100vh}
.header{background:linear-gradient(135deg,#6c63ff,#ff6584);padding:20px 30px;display:flex;align-items:center;justify-content:space-between}
.header h1{font-size:1.5rem;font-weight:700;color:#fff}
.badge{background:rgba(255,255,255,.2);color:#fff;padding:4px 12px;border-radius:20px;font-size:.8rem}
.container{max-width:1100px;margin:0 auto;padding:30px 20px}
.card{background:#1a1a2e;border-radius:12px;padding:24px;margin-bottom:24px;border:1px solid #2a2a4a}
.card h2{font-size:1rem;color:#aaa;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-grid label{display:flex;flex-direction:column;gap:6px;font-size:.85rem;color:#aaa}
.form-grid input,select{background:#0f0f1a;border:1px solid #3a3a5a;color:#e0e0e0;padding:10px 14px;border-radius:8px;font-size:.9rem}
.btn{background:linear-gradient(135deg,#6c63ff,#ff6584);color:#fff;border:none;padding:12px 28px;border-radius:8px;cursor:pointer;font-size:.95rem;font-weight:600;width:100%;margin-top:12px}
.btn:hover{opacity:.85}
.btn-sm{padding:6px 14px;border-radius:6px;font-size:.8rem;cursor:pointer;border:none;font-weight:600}
.btn-del{background:#ff4444;color:#fff}
.btn-view{background:#6c63ff;color:#fff}
.jobs-table{width:100%;border-collapse:collapse}
.jobs-table th{color:#888;font-size:.8rem;text-align:left;padding:8px 12px;border-bottom:1px solid #2a2a4a;text-transform:uppercase}
.jobs-table td{padding:10px 12px;border-bottom:1px solid #1e1e3a;font-size:.85rem;vertical-align:middle}
.status{display:inline-block;padding:3px 10px;border-radius:12px;font-size:.75rem;font-weight:700}
.status-completed{background:#1a4a1a;color:#4ade80}
.status-running{background:#1a3a4a;color:#60a5fa}
.status-queued{background:#3a3a1a;color:#facc15}
.status-failed{background:#4a1a1a;color:#f87171}
.topic{font-weight:600;color:#c4b5fd}
.step{color:#888;font-size:.78rem}
.yt-link{color:#ff6584;text-decoration:none;font-size:.8rem}
.yt-link:hover{text-decoration:underline}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}
.stat-card{background:#1a1a2e;border-radius:10px;padding:18px;border:1px solid #2a2a4a;text-align:center}
.stat-num{font-size:2rem;font-weight:700;color:#6c63ff}
.stat-label{font-size:.8rem;color:#888;margin-top:4px}
.alert{background:#2a1a4a;border:1px solid #6c63ff;border-radius:8px;padding:14px;margin-bottom:16px;font-size:.88rem;color:#c4b5fd}
#log{background:#0a0a16;border-radius:8px;padding:14px;font-size:.8rem;font-family:monospace;color:#4ade80;min-height:60px;max-height:120px;overflow-y:auto;margin-top:8px}
</style>
</head>
<body>
<div class="header">
  <h1>🎬 AI Shorts Automation</h1>
  <span class="badge" id="conn-badge">● 연결됨</span>
</div>
<div class="container">
  <div class="stats" id="stats">
    <div class="stat-card"><div class="stat-num" id="s-total">0</div><div class="stat-label">전체 작업</div></div>
    <div class="stat-card"><div class="stat-num" id="s-done">0</div><div class="stat-label">완료</div></div>
    <div class="stat-card"><div class="stat-num" id="s-run">0</div><div class="stat-label">진행 중</div></div>
    <div class="stat-card"><div class="stat-num" id="s-yt">0</div><div class="stat-label">유튜브 업로드</div></div>
  </div>

  <div class="card">
    <h2>새 숏츠 생성</h2>
    <div class="form-grid">
      <label>주제 (topic)<input id="f-topic" placeholder="예: AI 뉴스, 건강 비법" /></label>
      <label>채널명<input id="f-channel" placeholder="My Shorts Channel" /></label>
      <label>언어
        <select id="f-lang"><option value="ko">한국어</option><option value="en">English</option></select>
      </label>
      <label>길이(초)
        <select id="f-dur">
          <option value="15">15초</option>
          <option value="30">30초</option>
          <option value="45" selected>45초</option>
          <option value="60">60초</option>
        </select>
      </label>
    </div>
    <button class="btn" onclick="createJob()">🚀 생성 시작</button>
    <div id="log">대기 중...</div>
  </div>

  <div class="card">
    <h2>작업 목록</h2>
    <div id="jobs-container">로딩 중...</div>
  </div>
</div>

<script>
async function api(method, path, body) {
  const r = await fetch(path, {
    method, headers:{'Content-Type':'application/json'},
    body: body ? JSON.stringify(body) : undefined
  });
  return r.json();
}

async function createJob() {
  const topic = document.getElementById('f-topic').value.trim();
  const channel = document.getElementById('f-channel').value.trim() || 'AI Shorts';
  if (!topic) { alert('주제를 입력하세요'); return; }
  log('작업 생성 중...');
  const job = await api('POST', '/jobs', {
    topic, channel_name: channel,
    language: document.getElementById('f-lang').value,
    target_duration: parseInt(document.getElementById('f-dur').value)
  });
  log(`✅ 생성됨: ${job.id.slice(0,8)}... | 상태: ${job.status}`);
  setTimeout(loadJobs, 800);
  poll(job.id);
}

async function deleteJob(id) {
  if (!confirm('삭제할까요?')) return;
  await api('DELETE', '/jobs/' + id);
  loadJobs();
}

async function uploadYoutube(id) {
  if (!confirm('이 작업 영상을 유튜브에 업로드할까요?')) return;
  log('유튜브 업로드 시작...');
  try {
    const res = await api('POST', '/jobs/' + id + '/upload-youtube');
    if (res.youtube_url) {
      log('✅ 유튜브 업로드 완료: ' + res.youtube_url);
      loadJobs();
    } else {
      log('❌ 업로드 실패');
    }
  } catch (e) {
    log('❌ 업로드 요청 실패');
  }
}

function poll(id, count=0) {
  if (count > 60) return;
  setTimeout(async () => {
    const j = await api('GET', '/jobs/' + id);
    log(`[${j.id.slice(0,8)}] ${j.current_step} | ${j.status}`);
    loadJobs();
    if (j.status === 'running' || j.status === 'queued') poll(id, count+1);
    else if (j.status === 'completed') log('🎬 완료! final_short.mp4 생성됨');
    else if (j.status === 'failed') log('❌ 실패: ' + (j.error_message||''));
  }, 2000);
}

function log(msg) {
  const el = document.getElementById('log');
  el.textContent = new Date().toLocaleTimeString('ko') + ' ' + msg + '\\n' + el.textContent;
}

function statusBadge(s) {
  const map = {completed:'status-completed',running:'status-running',queued:'status-queued',failed:'status-failed'};
  return `<span class="status ${map[s]||''}">${s}</span>`;
}

async function loadJobs() {
  const jobs = await api('GET', '/jobs');
  const total = jobs.length;
  const done = jobs.filter(j=>j.status==='completed').length;
  const run = jobs.filter(j=>j.status==='running'||j.status==='queued').length;
  const yt = jobs.filter(j=>j.youtube_video_id).length;
  document.getElementById('s-total').textContent = total;
  document.getElementById('s-done').textContent = done;
  document.getElementById('s-run').textContent = run;
  document.getElementById('s-yt').textContent = yt;

  if (!jobs.length) { document.getElementById('jobs-container').innerHTML='<p style="color:#666;text-align:center;padding:20px">아직 작업이 없습니다</p>'; return; }
  let html = '<table class="jobs-table"><thead><tr><th>주제</th><th>채널</th><th>상태</th><th>단계</th><th>유튜브</th><th>생성일</th><th>작업</th></tr></thead><tbody>';
  for (const j of jobs) {
    const ytCell = j.youtube_url
      ? `<a class="yt-link" href="${j.youtube_url}" target="_blank">▶ 보기</a>`
      : '<span style="color:#555">-</span>';
    const uploadBtn = (!j.youtube_url && j.current_step === 'video_rendered')
      ? `<button class="btn-sm btn-view" onclick="uploadYoutube('${j.id}')">업로드</button>`
      : '';
    html += `<tr>
      <td class="topic">${j.topic}</td>
      <td>${j.channel_name}</td>
      <td>${statusBadge(j.status)}</td>
      <td class="step">${j.current_step}</td>
      <td>${ytCell}</td>
      <td style="color:#666;font-size:.75rem">${j.created_at.slice(0,16)}</td>
      <td>${uploadBtn} <button class="btn-sm btn-del" onclick="deleteJob('${j.id}')">삭제</button></td>
    </tr>`;
  }
  html += '</tbody></table>';
  document.getElementById('jobs-container').innerHTML = html;
}

loadJobs();
setInterval(loadJobs, 5000);
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)

# ─── Run ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n🎬 AI Shorts Automation v2.0")
    print("─" * 40)
    print("📊 Dashboard : http://localhost:8000")
    print("📚 API Docs  : http://localhost:8000/docs")
    print("▶ .env 설정 후 생성 직후 자동 업로드 또는 작업 목록의 업로드 버튼 사용")
    print("─" * 40)
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
