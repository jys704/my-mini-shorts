"""
AI Shorts Automation - Complete MVP
Run: python shorts_complete.py
Dashboard: http://localhost:8000
API Docs: http://localhost:8000/docs
"""
import os, json, uuid, sqlite3, subprocess, textwrap, threading
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

# ─── Config ───────────────────────────────────────────────────────────────────
BASE_DIR   = Path("shorts_data")
DB_PATH    = BASE_DIR / "app.db"
STORAGE    = BASE_DIR / "storage"
BASE_DIR.mkdir(exist_ok=True)
STORAGE.mkdir(exist_ok=True)

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
def split_scenes(script: str, duration: int) -> list:
    lines = [l.strip() for l in script.split("\n") if l.strip()]
    scenes = []
    per_scene = max(3, duration // max(len(lines), 1))
    for i, line in enumerate(lines):
        scenes.append({
            "scene": i + 1,
            "voice_line": line,
            "visual_prompt": f"신비롭고 역동적인 배경, 텍스트: '{line[:20]}...'",
            "duration": per_scene if i < len(lines) - 1 else 3
        })
    return scenes

# ─── TTS (ElevenLabs or silent) ───────────────────────────────────────────────
def synthesize_voice(text: str, out_path: Path) -> bool:
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
    # 무음 오디오 생성 (ffmpeg)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono",
        "-t", "3", str(out_path)
    ], capture_output=True)
    return False

# ─── FFmpeg Video Builder ─────────────────────────────────────────────────────
def build_video(job_id: str, scenes: list, script: str) -> Optional[Path]:
    job_dir = STORAGE / job_id
    video_dir = job_dir / "video"
    audio_dir = job_dir / "audio"
    video_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    scene_files = []

    for scene in scenes:
        idx = scene["scene"]
        dur = scene["duration"]
        text = scene["voice_line"]
        # 텍스트 줄바꿈 (25자)
        wrapped = "\n".join(textwrap.wrap(text, 22))
        scene_vid = video_dir / f"scene_{idx:02d}.mp4"
        audio_file = audio_dir / f"scene_{idx:02d}.mp3"

        # TTS 생성
        synthesize_voice(text, audio_file)

        # FFmpeg: 배경 + 텍스트 오버레이
        escaped = wrapped.replace("'", "\\'").replace(":", "\\:").replace("\n", "\\n")
        bg_color = ["0x1a1a2e", "0x16213e", "0x0f3460", "0x533483"][idx % 4]

        ret = subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c={bg_color}:size=1080x1920:rate=30",
            "-i", str(audio_file),
            "-vf",
            f"drawtext=text='{escaped}':fontcolor=white:fontsize=52:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=10:"
            f"borderw=3:bordercolor=black@0.8",
            "-t", str(dur),
            "-c:v", "libx264", "-c:a", "aac",
            "-shortest",
            str(scene_vid)
        ], capture_output=True, text=True)

        if ret.returncode != 0:
            # 오디오 없이 재시도
            subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"color=c={bg_color}:size=1080x1920:rate=30",
                "-vf",
                f"drawtext=text='{escaped}':fontcolor=white:fontsize=52:"
                f"x=(w-text_w)/2:y=(h-text_h)/2",
                "-t", str(dur), "-c:v", "libx264",
                str(scene_vid)
            ], capture_output=True)

        if scene_vid.exists():
            scene_files.append(scene_vid)

    if not scene_files:
        return None

    # concat
    concat_list = job_dir / "concat.txt"
    concat_list.write_text("\n".join(f"file '{f.resolve()}'" for f in scene_files))
    final = job_dir / "final_short.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy", str(final)
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

# ─── Pipeline ─────────────────────────────────────────────────────────────────
def run_pipeline(job_id: str, topic: str, channel: str, lang: str, duration: int):
    try:
        db_update(job_id, status="running", current_step="scripting")

        script = build_script(topic, duration, lang)
        db_update(job_id, script_text=script, current_step="scene_split")

        scenes = split_scenes(script, duration)
        db_update(job_id, scene_plan=json.dumps(scenes, ensure_ascii=False), current_step="rendering")

        final_vid = build_video(job_id, scenes, script)
        manifest = {
            "job_id": job_id,
            "final_video": str(final_vid) if final_vid else None,
            "final_video_ready": final_vid is not None and final_vid.exists(),
            "scenes": len(scenes),
            "youtube": {"status": "pending"}
        }
        db_update(job_id, asset_manifest=json.dumps(manifest, ensure_ascii=False),
                  current_step="video_rendered" if final_vid else "render_failed",
                  status="completed" if final_vid else "failed")

        # YouTube 자동 업로드 (키 설정 시)
        if final_vid and YT_CLIENT_ID:
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
  let html = '<table class="jobs-table"><thead><tr><th>주제</th><th>채널</th><th>상태</th><th>단계</th><th>유튜브</th><th>생성일</th><th></th></tr></thead><tbody>';
  for (const j of jobs) {
    const ytCell = j.youtube_url
      ? `<a class="yt-link" href="${j.youtube_url}" target="_blank">▶ 보기</a>`
      : '<span style="color:#555">-</span>';
    html += `<tr>
      <td class="topic">${j.topic}</td>
      <td>${j.channel_name}</td>
      <td>${statusBadge(j.status)}</td>
      <td class="step">${j.current_step}</td>
      <td>${ytCell}</td>
      <td style="color:#666;font-size:.75rem">${j.created_at.slice(0,16)}</td>
      <td><button class="btn-sm btn-del" onclick="deleteJob('${j.id}')">삭제</button></td>
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
    print("─" * 40)
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
