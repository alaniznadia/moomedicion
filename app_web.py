#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app_web.py - Versión web del editor automático de video.

Subís un video desde el navegador, se procesa en el servidor y descargás las
3 versiones (viral, premium, comercial) con sus subtítulos y el informe.

Local:        python app_web.py            ->  http://localhost:8000
Producción:   gunicorn -w 1 --threads 8 --timeout 0 -b 0.0.0.0:8000 app_web:app

Variables de entorno (todas opcionales, salvo APP_PASSWORD si lo publicás):
    APP_PASSWORD       contraseña de acceso (usuario: cualquiera). OBLIGATORIA en internet.
    BRAND_FILE         ruta a un brand.json por defecto
    MUSIC_DIR          carpeta con música con licencia comercial   (def.: ./musica)
    SFX_DIR            carpeta con efectos de sonido               (def.: ./sfx)
    DATA_DIR           dónde guardar los trabajos                  (def.: ./trabajos)
    WHISPER_MODEL      tiny | base | small | medium | large-v3     (def.: small)
    MAX_UPLOAD_MB      tamaño máximo de subida                     (def.: 500)
    MAX_VIDEO_SECONDS  duración máxima del video                   (def.: 180)
    KEEP_HOURS         horas que se conservan los trabajos         (def.: 24)
    ANTHROPIC_API_KEY  para generar título, descripción y gancho
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from editor_video import PROFILES, load_brand, probe, process_video

BASE = Path(os.getenv("DATA_DIR", "trabajos")).resolve()
BASE.mkdir(parents=True, exist_ok=True)
MUSIC_DIR = Path(os.getenv("MUSIC_DIR", "musica"))
SFX_DIR = Path(os.getenv("SFX_DIR", "sfx"))
BRAND_FILE = os.getenv("BRAND_FILE") or ("brand.json" if Path("brand.json").is_file() else None)
PASSWORD = os.getenv("APP_PASSWORD")
MODEL = os.getenv("WHISPER_MODEL", "small")
MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
MAX_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", "180"))
KEEP_HOURS = float(os.getenv("KEEP_HOURS", "24"))
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
LOGO_EXT = {".png", ".webp"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
RENDER_LOCK = threading.Lock()   # un solo render a la vez: FFmpeg usa mucha CPU y memoria
JOB_ID = re.compile(r"^[0-9a-f]{32}$")


# --------------------------------------------------------------------------
# Acceso
# --------------------------------------------------------------------------
@app.before_request
def require_password():
    if not PASSWORD:
        return None
    auth = request.authorization
    if auth and secrets.compare_digest(auth.password or "", PASSWORD):
        return None
    return Response("Acceso restringido.", 401, {"WWW-Authenticate": 'Basic realm="Editor de video"'})


@app.errorhandler(413)
def too_big(_):
    return jsonify(error=f"El archivo supera el máximo de {MAX_MB} MB."), 413


# --------------------------------------------------------------------------
# Trabajos
# --------------------------------------------------------------------------
def run_job(job_id: str, params: dict) -> None:
    job = JOBS[job_id]
    log = lambda m: job["log"].append(str(m).strip())   # noqa: E731
    try:
        job["estado"] = "en_cola"
        with RENDER_LOCK:
            job["estado"] = "procesando"
            info = probe(params["video"])
            if info["duration"] > MAX_SECONDS:
                raise RuntimeError(
                    f"El video dura {info['duration']:.0f} s y el máximo permitido es {MAX_SECONDS} s.")
            res = process_video(
                params["video"], params["out"], brand=params["brand"],
                music_dir=str(MUSIC_DIR) if MUSIC_DIR.is_dir() else None,
                sfx_dir=str(SFX_DIR) if SFX_DIR.is_dir() else None,
                hook=params["hook"], texts=params["texts"], reframe=params["reframe"],
                model=MODEL, log=log)
        files = sorted(p.name for p in Path(params["out"]).iterdir()
                       if p.suffix in {".mp4", ".srt"} or p.name == "entrega.md")
        job["archivos"] = files
        job["voz"] = res["voz"]
        job["versiones"] = {
            PROFILES[k].filename: {"decisiones": v["decisiones"], "calidad": v["calidad"]}
            for k, v in res["versiones"].items()}
        job["texto_publicacion"] = res.get("texto_publicacion")
        job["estado"] = "listo"
    except BaseException as exc:      # incluye SystemExit de check_tools()
        job["estado"] = "error"
        job["error"] = str(exc) or exc.__class__.__name__


def build_brand_file(job_dir: Path, form, logo) -> str | None:
    brand = load_brand(BRAND_FILE) if BRAND_FILE else {}
    if logo and logo.filename and Path(logo.filename).suffix.lower() in LOGO_EXT:
        p = job_dir / ("logo" + Path(logo.filename).suffix.lower())
        logo.save(p)
        brand["logo"] = str(p)
    for form_key, brand_key in (("cta", "cta"), ("contacto", "contacto"),
                                ("color_acento", "color_acento"), ("nombre", "nombre")):
        val = (form.get(form_key) or "").strip()
        if val:
            brand[brand_key] = val
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", brand.get("color_acento", "") or ""):
        brand["color_acento"] = "#" + brand["color_acento"].lstrip("#")
    else:
        brand.pop("color_acento", None)
    if not brand:
        return None
    path = job_dir / "brand.json"
    path.write_text(json.dumps(brand, ensure_ascii=False), encoding="utf-8")
    return str(path)


@app.post("/procesar")
def procesar():
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(error="Elegí un video."), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in VIDEO_EXT:
        return jsonify(error="Formato no soportado. Usá MP4, MOV, M4V, WEBM, MKV o AVI."), 400

    job_id = uuid.uuid4().hex
    job_dir = BASE / job_id
    job_dir.mkdir(parents=True)
    video = job_dir / f"original{ext}"
    f.save(video)

    reframe = request.form.get("reframe", "crop")
    params = {
        "video": str(video), "out": str(job_dir / "salida"),
        "brand": build_brand_file(job_dir, request.form, request.files.get("logo")),
        "hook": (request.form.get("hook") or "").strip() or None,
        "texts": [t for t in (request.form.get("texts") or "").split("|") if t.strip()],
        "reframe": reframe if reframe in ("crop", "blur") else "crop",
    }
    with JOBS_LOCK:
        JOBS[job_id] = {"estado": "en_cola", "log": [], "creado": time.time()}
    threading.Thread(target=run_job, args=(job_id, params), daemon=True).start()
    return jsonify(id=job_id)


@app.get("/estado/<job_id>")
def estado(job_id: str):
    job = JOBS.get(job_id) if JOB_ID.match(job_id) else None
    if not job:
        return jsonify(error="Trabajo no encontrado (puede haber vencido)."), 404
    return jsonify({k: v for k, v in job.items() if k != "creado"})


@app.get("/archivo/<job_id>/<path:name>")
def archivo(job_id: str, name: str):
    if not JOB_ID.match(job_id) or job_id not in JOBS:
        return jsonify(error="No encontrado."), 404
    inline = request.args.get("ver") == "1"
    return send_from_directory(BASE / job_id / "salida", name,
                               as_attachment=not inline, conditional=True)


def cleanup_loop() -> None:
    while True:
        time.sleep(3600)
        limit = time.time() - KEEP_HOURS * 3600
        for job_id, job in list(JOBS.items()):
            if job.get("creado", 0) < limit and job.get("estado") in ("listo", "error"):
                shutil.rmtree(BASE / job_id, ignore_errors=True)
                JOBS.pop(job_id, None)
        for d in BASE.iterdir():            # restos de reinicios anteriores
            if d.is_dir() and d.name not in JOBS and d.stat().st_mtime < limit:
                shutil.rmtree(d, ignore_errors=True)


threading.Thread(target=cleanup_loop, daemon=True).start()


# --------------------------------------------------------------------------
# Página
# --------------------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Editor automático de video</title>
<style>
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6b6b70;--line:#e2e2e6;--card:#f6f6f8;--ac:#6d28d9;--ok:#15803d;--bad:#b91c1c}
@media(prefers-color-scheme:dark){:root{--bg:#141416;--fg:#f2f2f4;--mut:#a0a0a8;--line:#2d2d33;--card:#1c1c20;--ac:#a78bfa;--ok:#4ade80;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:820px;margin:0 auto;padding:24px 16px 64px}h1{font-size:1.5rem;margin:.2em 0}
p.sub{color:var(--mut);margin-top:0}label{display:block;font-weight:600;margin:14px 0 4px}
input[type=text],select,input[type=file]{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:inherit}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:600px){.row{grid-template-columns:1fr}}
details{margin-top:16px;border:1px solid var(--line);border-radius:8px;padding:8px 12px}summary{cursor:pointer;font-weight:600}
small{color:var(--mut)}button{margin-top:20px;width:100%;padding:14px;border:0;border-radius:10px;background:var(--ac);color:#fff;font:600 1rem system-ui;cursor:pointer}
button:disabled{opacity:.5;cursor:wait}#bar{height:8px;background:var(--card);border-radius:4px;overflow:hidden;margin-top:16px;display:none}
#bar i{display:block;height:100%;width:0;background:var(--ac);transition:width .3s}
#log{white-space:pre-wrap;color:var(--mut);font-size:.9rem;margin-top:12px}.err{color:var(--bad);font-weight:600}
.v{border:1px solid var(--line);border-radius:12px;padding:12px;margin-top:16px;background:var(--card)}
.v video{width:100%;max-height:520px;border-radius:8px;background:#000}.v ul{padding-left:18px;margin:8px 0}
.ok{color:var(--ok)}.bad{color:var(--bad)}a.dl{display:inline-block;margin:6px 12px 0 0;color:var(--ac);font-weight:600}
</style></head><body><main>
<h1>Editor automático de video</h1>
<p class="sub">Subí un video y recibís 3 versiones listas para publicar: viral, premium y comercial.</p>
<form id="f">
<label>Video</label><input type="file" name="video" accept="video/*" required>
<small>MP4, MOV, WEBM... Con voz o sin voz.</small>
<details><summary>Opciones (opcionales)</summary>
<label>Gancho de los primeros segundos</label><input type="text" name="hook" placeholder="Ej: Mirá este cambio">
<label>Textos en pantalla (solo para videos SIN voz)</label><input type="text" name="texts" placeholder="Antes | Proceso | Resultado final">
<small>Separados por la barra |</small>
<label>Video horizontal</label><select name="reframe"><option value="crop">Recortar al centro</option><option value="blur">Mostrar completo con fondo desenfocado</option></select>
<div class="row"><div><label>Frase de cierre (versión comercial)</label><input type="text" name="cta" placeholder="Escribinos por WhatsApp"></div>
<div><label>Datos de contacto</label><input type="text" name="contacto" placeholder="WhatsApp ... | @tu_cuenta"></div></div>
<div class="row"><div><label>Color de resaltado</label><input type="text" name="color_acento" placeholder="#FFD400"></div>
<div><label>Logo (PNG)</label><input type="file" name="logo" accept="image/png,image/webp"></div></div>
</details>
<button id="go" type="submit">Editar video</button></form>
<div id="bar"><i></i></div><div id="log"></div><div id="out"></div>
<script>
const $=s=>document.querySelector(s),f=$('#f'),go=$('#go'),bar=$('#bar'),fill=bar.firstElementChild,logEl=$('#log'),out=$('#out');
function msg(t,cls){logEl.textContent=t;logEl.className=cls||''}
f.addEventListener('submit',e=>{e.preventDefault();out.replaceChildren();go.disabled=true;bar.style.display='block';fill.style.width='0';msg('Subiendo video...');
const x=new XMLHttpRequest();x.open('POST','/procesar');
x.upload.onprogress=p=>{if(p.lengthComputable){fill.style.width=(p.loaded/p.total*40)+'%'}};
x.onload=()=>{let r={};try{r=JSON.parse(x.responseText)}catch(_){}
 if(x.status!==200||!r.id){fail(r.error||'No se pudo subir el video.');return}
 poll(r.id)};x.onerror=()=>fail('Error de conexión al subir el video.');x.send(new FormData(f))});
function fail(t){msg(t,'err');go.disabled=false;bar.style.display='none'}
async function poll(id){
 try{const r=await fetch('/estado/'+id);const j=await r.json();
  if(!r.ok){fail(j.error);return}
  if(j.estado==='error'){fail(j.error||'Falló el procesamiento.');return}
  const names={en_cola:'En cola...',procesando:'Procesando...'};
  msg((names[j.estado]||'')+'\\n'+(j.log||[]).join('\\n'));
  fill.style.width=(40+Math.min(55,(j.log||[]).length*14))+'%';
  if(j.estado==='listo'){done(id,j);return}
 }catch(_){}
 setTimeout(()=>poll(id),2500)}
function el(t,c,txt){const n=document.createElement(t);if(c)n.className=c;if(txt!==undefined)n.textContent=txt;return n}
function done(id,j){go.disabled=false;fill.style.width='100%';msg('Listo.');
 for(const [name,v] of Object.entries(j.versiones)){
  const box=el('div','v');box.append(el('strong','',name));
  const vid=el('video');vid.controls=true;vid.preload='metadata';vid.src=`/archivo/${id}/${name}?ver=1`;box.append(vid);
  const a=el('a','dl','Descargar MP4');a.href=`/archivo/${id}/${name}`;box.append(a);
  const srt=name.replace('.mp4','.srt');if((j.archivos||[]).includes(srt)){const s=el('a','dl','Subtítulos (.srt)');s.href=`/archivo/${id}/${srt}`;box.append(s)}
  const q=v.calidad;box.append(el('p',q.ok?'ok':'bad',q.ok?'Verificación de calidad: OK':'Revisar: '+q.problemas.join(' ')));
  const ul=el('ul');v.decisiones.forEach(d=>ul.append(el('li','',d)));box.append(ul);out.append(box)}
 const t=j.texto_publicacion;if(t){const b=el('div','v');b.append(el('strong','','Texto para publicar'));
  [['Título',t.titulo],['Descripción',t.descripcion_instagram],['Gancho alternativo',t.gancho_alternativo],['Recomendada',(t.version_recomendada||'')+': '+(t.motivo||'')]]
  .forEach(([k,v])=>{const p=el('p');p.append(el('strong','',k+': '),document.createTextNode(v||''));b.append(p)});out.append(b)}
 const r=el('a','dl','Informe completo (entrega.md)');r.href=`/archivo/${id}/entrega.md`;out.append(r)}
</script></main></body></html>"""


@app.get("/")
def home():
    return Response(PAGE, mimetype="text/html")


@app.get("/salud")
def salud():
    return "ok"


if __name__ == "__main__":
    if not PASSWORD:
        print("AVISO: no definiste APP_PASSWORD. Está bien para uso local; "
              "NO publiques esto en internet sin contraseña.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), threaded=True)
