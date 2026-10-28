#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
editor_video.py - Editor automático de video para Reels / TikTok / Shorts.

Recibe UN video y genera TRES versiones 9:16 (1080x1920, H.264/AAC):
    VIDEO_VIRAL.mp4  |  VIDEO_PREMIUM.mp4  |  VIDEO_COMERCIAL.mp4
más subtítulos .srt, un informe de decisiones (entrega.md) y una verificación
de calidad automática de cada archivo exportado.

Requisitos:
    - Python 3.10+
    - FFmpeg y FFprobe en el PATH (con libass, que viene en las builds estándar)
    - pip install faster-whisper          (transcripción y detección de voz)
    - pip install anthropic               (opcional: título, descripción y gancho)

Uso básico:
    python editor_video.py mi_video.mp4
    python editor_video.py mi_video.mp4 --brand brand.json --music ./musica --sfx ./sfx

Estructura sugerida de carpetas (todo opcional):
    musica/                 pistas con licencia comercial (.mp3/.wav/.m4a)
        viral/ premium/ comercial/   (subcarpetas opcionales, una por versión)
    sfx/                    efectos cortos (whoosh1.wav, pop.wav, ...)
    brand.json              kit de marca (ver brand.example.json)

El módulo también se puede importar:  process_video(...) devuelve un dict con
las rutas y el resultado de la verificación (útil para envolverlo en una web).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

W, H = 1080, 1920
FILLERS = {"eh", "ehh", "em", "emm", "mm", "mmm", "ehm", "hmm", "ah", "ahh"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


# --------------------------------------------------------------------------
# Perfiles de las tres versiones
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Profile:
    key: str
    filename: str
    min_s: float          # duración objetivo mínima
    target_max: float     # duración objetivo máxima
    hard_max: float       # límite absoluto
    gap: float            # pausa (s) a partir de la cual se corta
    pad_in: float         # aire antes de cada frase
    pad_out: float        # aire después de cada frase
    music_vol: float      # volumen de la música (relativo a la voz)
    zoom_amp: float       # intensidad del punch-in (0.10 = 10 %)
    piece: float          # largo máximo (s) de un plano antes de variar el encuadre
    font_size: int
    bold: int
    outline: int
    margin_v: int
    chunk: int            # palabras por subtítulo
    highlight: bool       # resaltar la palabra que se dice
    upper: bool
    pop: bool
    sfx: bool
    hook: bool
    cta: bool
    grade: str            # cadena de filtros FFmpeg de color


PROFILES: dict[str, Profile] = {
    "viral": Profile(
        "viral", "VIDEO_VIRAL.mp4", 15, 30, 45,
        gap=0.30, pad_in=0.06, pad_out=0.12, music_vol=0.22, zoom_amp=0.12, piece=3.2,
        font_size=80, bold=1, outline=6, margin_v=620, chunk=3,
        highlight=True, upper=True, pop=True, sfx=True, hook=True, cta=False,
        grade="eq=contrast=1.08:saturation=1.15,unsharp=5:5:0.6:5:5:0.0",
    ),
    "premium": Profile(
        "premium", "VIDEO_PREMIUM.mp4", 30, 45, 60,
        gap=0.55, pad_in=0.12, pad_out=0.25, music_vol=0.15, zoom_amp=0.04, piece=7.0,
        font_size=52, bold=0, outline=2, margin_v=520, chunk=6,
        highlight=False, upper=False, pop=False, sfx=False, hook=False, cta=False,
        grade=("hqdn3d=2:1.5:3:3,eq=contrast=1.06:saturation=0.95,"
               "colorbalance=rs=0.03:bs=-0.03:rh=0.04:bh=-0.04"),
    ),
    "comercial": Profile(
        "comercial", "VIDEO_COMERCIAL.mp4", 20, 40, 60,
        gap=0.40, pad_in=0.08, pad_out=0.15, music_vol=0.20, zoom_amp=0.08, piece=4.5,
        font_size=70, bold=1, outline=5, margin_v=560, chunk=4,
        highlight=True, upper=False, pop=True, sfx=True, hook=True, cta=True,
        grade="eq=contrast=1.06:saturation=1.10,unsharp=5:5:0.5:5:5:0.0",
    ),
}


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=cwd)
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-15:])
        hint = ""
        if p.returncode in (-9, 137):
            hint = "\n(El proceso se quedó sin memoria: probá con un video más corto o más liviano.)"
        raise RuntimeError(f"Falló {cmd[0]} (código {p.returncode}).\n{tail}{hint}")
    return p


def check_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"No encuentro '{tool}'. Instalá FFmpeg y agregalo al PATH.")
    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                             capture_output=True, text=True).stdout
    if not re.search(r"\bass\b", filters):
        raise SystemExit("Tu FFmpeg no incluye libass (filtro 'ass'). "
                         "Instalá una build completa (gyan.dev en Windows, brew en Mac).")


def probe(path: Path) -> dict:
    d = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(path)]).stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    if v is None:
        raise SystemExit("El archivo no tiene pista de video.")
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    num, _, den = v.get("r_frame_rate", "30/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    try:
        rot = int(v.get("tags", {}).get("rotate", 0))
    except ValueError:
        pass
    for sd in v.get("side_data_list", []):
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if abs(rot) in (90, 270):
        w, h = h, w
    return {
        "width": w, "height": h, "fps": fps,
        "duration": float(d["format"]["duration"]),
        "has_audio": a is not None,
        "vcodec": v.get("codec_name"), "acodec": a.get("codec_name") if a else None,
    }


def hex_style(h: str) -> str:      # #RRGGBB -> &H00BBGGRR (estilos ASS)
    h = h.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}"


def hex_inline(h: str) -> str:     # #RRGGBB -> &HBBGGRR& (overrides ASS)
    h = h.lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&"


def ass_time(t: float) -> str:
    t = max(t, 0.0)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def srt_time(t: float) -> str:
    ms = int(round(max(t, 0.0) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def esc(t: str) -> str:
    return re.sub(r"[{}\\]", "", t).replace("\n", " ").strip()


def is_filler(word: str) -> bool:
    return re.sub(r"\W", "", word.lower()) in FILLERS


# --------------------------------------------------------------------------
# Kit de marca
# --------------------------------------------------------------------------
def load_brand(path: str | None) -> dict:
    if not path:
        return {}
    p = Path(path).resolve()
    b = json.loads(p.read_text(encoding="utf-8"))
    for k in ("logo", "fuentes_dir"):
        if b.get(k):
            q = Path(b[k])
            b[k] = str(q if q.is_absolute() else p.parent / q)
    return b


# --------------------------------------------------------------------------
# Transcripción y cortes
# --------------------------------------------------------------------------
def transcribe(video: Path, model_name: str, lang: str) -> list[dict]:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit("Falta faster-whisper:  pip install faster-whisper")
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
             "-vn", "-ac", "1", "-ar", "16000", str(wav)])
        model = WhisperModel(model_name, device="auto", compute_type="auto")
        segments, _ = model.transcribe(
            str(wav), language=lang, word_timestamps=True, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300})
        words = []
        for seg in segments:
            for w in (seg.words or []):
                t = w.word.strip()
                if t:
                    words.append({"w": t, "s": float(w.start), "e": float(w.end)})
    return words


def build_segments(words: list[dict], prof: Profile, dur: float) -> list[tuple[float, float]]:
    clean = [w for w in words if not is_filler(w["w"])]
    runs: list[list[float]] = []
    for w in clean:
        if runs and w["s"] - runs[-1][1] <= prof.gap:
            runs[-1][1] = w["e"]
        else:
            runs.append([w["s"], w["e"]])
    merged: list[list[float]] = []
    for s, e in runs:
        s, e = max(0.0, s - prof.pad_in), min(dur, e + prof.pad_out)
        if merged and s <= merged[-1][1] + 0.02:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e - s >= 0.25]


def cap_segments(segs, cap: float):
    out, tot = [], 0.0
    for s, e in segs:
        if tot + (e - s) > cap:
            if not out:
                out.append((s, s + cap))
            return out, True
        out.append((s, e))
        tot += e - s
    return out, False


def remap_words(words: list[dict], segs) -> list[dict]:
    out, off = [], 0.0
    for s, e in segs:
        for w in words:
            if is_filler(w["w"]):
                continue
            if w["s"] >= s - 0.02 and w["e"] <= e + 0.05:
                out.append({"w": w["w"],
                            "s": off + max(0.0, w["s"] - s),
                            "e": off + min(e - s, w["e"] - s)})
        off += e - s
    return out


def chunk_words(words: list[dict], n: int) -> list[list[dict]]:
    chunks, cur = [], []
    for w in words:
        if cur and w["s"] - cur[-1]["e"] > 0.4:
            chunks.append(cur)
            cur = []
        cur.append(w)
        if len(cur) >= n or re.search(r"[.!?…]$", w["w"]):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


# --------------------------------------------------------------------------
# Subtítulos (ASS para el video, SRT como archivo suelto)
# --------------------------------------------------------------------------
def build_ass(rw, prof: Profile, brand: dict, D: float, hook: str | None,
              texts: list[str], cta_start: float | None) -> str:
    font = brand.get("fuente") or "Arial"
    accent = brand.get("color_acento") or "#FFD400"
    base = brand.get("color_principal") or "#FFFFFF"
    base_in, acc_in = hex_inline(base), hex_inline(accent)

    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n" % (W, H))
    styles = (
        f"Style: Sub,{font},{prof.font_size},{hex_style(base)},&H000000FF,&H00000000,&H80000000,"
        f"{prof.bold},0,0,0,100,100,0,0,1,{prof.outline},0,2,80,80,{prof.margin_v},1\n"
        f"Style: Hook,{font},76,{hex_style(base)},&H000000FF,&H00000000,&H80000000,"
        f"1,0,0,0,100,100,0,0,1,6,0,8,80,80,420,1\n"
        f"Style: Cta,{font},68,{hex_style(accent)},&H000000FF,&H00000000,&H80000000,"
        f"1,0,0,0,100,100,0,0,1,5,0,5,80,80,0,1\n")
    ev = ["\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    def fmt(w: str) -> str:
        w = esc(w)
        if prof.upper:
            w = re.sub(r"[.,;:¡¿]", "", w).upper()
        return w

    def line(s, e, style, text):
        ev.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},{style},,0,0,0,,{text}")

    chunks = chunk_words(rw, prof.chunk)
    for ci, ch in enumerate(chunks):
        nxt = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else D
        if prof.highlight:
            for j, w in enumerate(ch):
                s = w["s"]
                e = ch[j + 1]["s"] if j + 1 < len(ch) else min(w["e"] + 0.12, nxt)
                if e <= s:
                    e = s + 0.1
                parts = []
                for k, x in enumerate(ch):
                    c = acc_in if k == j else base_in
                    parts.append("{\\c%s}%s" % (c, fmt(x["w"])))
                pop = "{\\fscx88\\fscy88\\t(0,90,\\fscx100\\fscy100)}" if (prof.pop and j == 0) else ""
                line(s, e, "Sub", pop + " ".join(parts))
        else:
            s, e = ch[0]["s"], min(ch[-1]["e"] + 0.2, nxt)
            line(s, e, "Sub", "{\\fad(120,120)}" + " ".join(fmt(x["w"]) for x in ch))

    if texts:  # videos sin voz: textos en pantalla repartidos
        slot = D / len(texts)
        for i, t in enumerate(texts):
            s = i * slot + 0.4
            line(s, min(s + 2.6, (i + 1) * slot), "Sub", "{\\fad(150,150)}" + fmt(t))

    if hook and prof.hook:
        line(0.0, min(2.5, D), "Hook", "{\\fad(0,200)}" + esc(hook))

    if cta_start is not None:
        cta, contact = brand.get("cta", ""), brand.get("contacto", "")
        txt = esc(cta) + (("\\N{\\fs46\\c%s}" % base_in) + esc(contact) if contact else "")
        ev.append(f"Dialogue: 1,{ass_time(cta_start)},{ass_time(D)},Cta,,0,0,0,,{{\\fad(250,0)}}{txt}")

    return head + styles + "\n".join(ev) + "\n"


def build_srt(rw: list[dict]) -> str:
    out = []
    for i, ch in enumerate(chunk_words(rw, 7), 1):
        out.append(f"{i}\n{srt_time(ch[0]['s'])} --> {srt_time(ch[-1]['e'] + 0.1)}\n"
                   f"{' '.join(w['w'] for w in ch)}\n")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Planos, zooms y filtros FFmpeg
# --------------------------------------------------------------------------
def plan_pieces(segs, prof: Profile):
    """Divide los tramos en planos y alterna encuadres (punch-in) para variar."""
    total = sum(e - s for s, e in segs)
    piece = max(prof.piece, total / 40)          # máx. ~40 planos
    parts = []
    for s, e in segs:
        L = e - s
        n = max(1, round(L / piece)) if L > piece * 1.6 else 1
        step = L / n
        parts += [(s + j * step, s + (j + 1) * step) for j in range(n)]
    return [(s, e, prof.zoom_amp if (prof.zoom_amp > 0 and k % 2 == 1) else 0.0)
            for k, (s, e) in enumerate(parts)]


def video_piece_chain(i: int, s: float, e: float, z: float, blur_mode: bool) -> str:
    chain = f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS"
    if z > 0:
        k = 1 + z
        chain += f",crop=iw/{k:.4f}:ih/{k:.4f}"
    if blur_mode:
        # el fondo desenfocado se arma una sola vez después de unir los planos
        return f"{chain},scale={W}:-2,setsar=1[v{i}]"
    return (f"{chain},scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},setsar=1[v{i}]")


def pick_music(music_dir, name, used: set, seed: str):
    if not music_dir:
        return None
    d = Path(music_dir)
    cands = []
    if (d / name).is_dir():
        cands = [p for p in (d / name).iterdir() if p.suffix.lower() in AUDIO_EXT]
    if not cands and d.is_dir():
        cands = [p for p in d.iterdir() if p.suffix.lower() in AUDIO_EXT]
    cands = sorted(cands)
    fresh = [c for c in cands if c not in used]
    cands = fresh or cands
    if not cands:
        return None
    choice = random.Random(seed + name).choice(cands)
    used.add(choice)
    return choice


def list_sfx(sfx_dir):
    if not sfx_dir or not Path(sfx_dir).is_dir():
        return []
    return sorted(p for p in Path(sfx_dir).iterdir() if p.suffix.lower() in AUDIO_EXT)


# --------------------------------------------------------------------------
# Verificación de calidad del archivo exportado
# --------------------------------------------------------------------------
def verify(path: Path, prof: Profile, expect_voice_cut: bool) -> dict:
    info = probe(path)
    issues = []
    if (info["width"], info["height"]) != (W, H):
        issues.append(f"Resolución {info['width']}x{info['height']} (se esperaba {W}x{H}).")
    if info["vcodec"] != "h264":
        issues.append(f"Códec de video {info['vcodec']} (se esperaba h264).")
    if info["acodec"] != "aac":
        issues.append(f"Códec de audio {info['acodec']} (se esperaba aac).")
    if info["duration"] > prof.hard_max + 0.5:
        issues.append(f"Dura {info['duration']:.1f}s y el máximo es {prof.hard_max:.0f}s.")
    if info["duration"] < 1.0:
        issues.append("El video es demasiado corto.")
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "silencedetect=n=-45dB:d=1.0", "-vf", "blackdetect=d=0.3:pic_th=0.98",
         "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    sil = len(re.findall("silence_start", p.stderr))
    blk = len(re.findall("black_start", p.stderr))
    if sil:
        issues.append(f"Se detectaron {sil} silencio(s) de más de 1 s.")
    if blk:
        issues.append(f"Se detectaron {blk} tramo(s) de pantalla negra.")
    return {"duracion": round(info["duration"], 1), "ok": not issues, "problemas": issues}


# --------------------------------------------------------------------------
# Render de una versión
# --------------------------------------------------------------------------
def render_version(prof: Profile, c: dict) -> dict:
    info, voice, words, brand = c["info"], c["voice"], c["words"], c["brand"]
    dur = info["duration"]
    notes = []

    if voice:
        segs = build_segments(words, prof, dur) or [(0.0, dur)]
        segs, truncated = cap_segments(segs, prof.hard_max)
        total = sum(e - s for s, e in segs)
        speed, D = 1.0, total
        rw = remap_words(words, segs)
        if truncated:
            notes.append(f"El contenido superaba {prof.hard_max:.0f}s: se cortó al final para respetar el límite.")
        if D < prof.min_s:
            notes.append(f"Quedó en {D:.0f}s, por debajo del objetivo de {prof.min_s:.0f}s: el material original es corto.")
    else:
        segs = [(0.0, dur)]
        total = dur
        speed = min(2.0, dur / prof.target_max) if dur > prof.target_max else 1.0
        D = min(dur / speed, prof.hard_max)
        rw = []
        notes.append("Video sin voz: se usaron textos en pantalla y la música marca el ritmo.")

    pieces = plan_pieces(segs, prof)
    src_vertical = info["width"] / info["height"] <= 9 / 16 + 0.02

    # tiempos de cada plano en la línea de tiempo de salida (para efectos de sonido)
    starts, t = [], 0.0
    for s, e, z in pieces:
        starts.append(t / speed)
        t += e - s

    # ---- entradas extra
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(c["video"])]
    nxt = 1
    logo_idx = music_idx = None
    if brand.get("logo") and Path(brand["logo"]).is_file():
        cmd += ["-i", brand["logo"]]
        logo_idx, nxt = nxt, nxt + 1
    music = pick_music(c["music_dir"], prof.key, c["used_music"], c["seed"])
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        music_idx, nxt = nxt, nxt + 1
    sfx_files = list_sfx(c["sfx_dir"]) if prof.sfx else []
    sfx_times = [max(0.0, st - 0.05) for (st, p) in zip(starts, pieces) if p[2] > 0][:6] if sfx_files else []
    sfx_idx = []
    for k, _ in enumerate(sfx_times):
        cmd += ["-i", str(sfx_files[k % len(sfx_files)])]
        sfx_idx.append(nxt)
        nxt += 1

    # ---- video
    g = []
    blur_mode = c["reframe"] == "blur" and not src_vertical
    for i, (s, e, z) in enumerate(pieces):
        g.append(video_piece_chain(i, s, e, z, blur_mode))
    g.append("".join(f"[v{i}]" for i in range(len(pieces))) +
             f"concat=n={len(pieces)}:v=1:a=0[cv]")
    vcur = "cv"
    if blur_mode:
        g.append("[cv]split=2[bgs][fgs]")
        g.append(f"[bgs]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                 "boxblur=40:5,eq=brightness=-0.1[bgb]")
        g.append("[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[cvb]")
        vcur = "cvb"
    if speed > 1.001:
        g.append(f"[{vcur}]setpts=PTS/{speed:.4f}[cvs]")
        vcur = "cvs"
    g.append(f"[{vcur}]{prof.grade}[gr]")
    vcur = "gr"
    if logo_idx is not None:
        g.append(f"[{logo_idx}:v]scale=150:-1,format=rgba,colorchannelmixer=aa=0.85[lg]")
        g.append(f"[{vcur}][lg]overlay=W-w-60:270[wm]")
        vcur = "wm"

    has_cta = prof.cta and (brand.get("cta") or brand.get("contacto"))
    cta_start = None
    if has_cta:
        cta_start = D - min(3.0, D * 0.35)
        g.append(f"[{vcur}]drawbox=x=0:y=ih*0.42:w=iw:h=ih*0.16:color=black@0.55:t=fill:"
                 f"enable='gte(t,{cta_start:.2f})'[bx]")
        vcur = "bx"

    work = Path(tempfile.mkdtemp(prefix=f"ev_{prof.key}_"))
    try:
        ass_text = build_ass(rw, prof, brand, D, c["hook"], c["texts"] if not voice else [], cta_start)
        (work / "subs.ass").write_text(ass_text, encoding="utf-8")
        ass_filter = "ass=subs.ass"
        if brand.get("fuentes_dir") and Path(brand["fuentes_dir"]).is_dir():
            shutil.copytree(brand["fuentes_dir"], work / "fonts")
            ass_filter += ":fontsdir=fonts"
        g.append(f"[{vcur}]{ass_filter}[vout]")

        # ---- audio
        if info["has_audio"]:
            for i, (s, e) in enumerate(segs):
                g.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
            g.append("".join(f"[a{i}]" for i in range(len(segs))) +
                     f"concat=n={len(segs)}:v=0:a=1[ca]")
        else:
            g.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{total:.3f},asetpts=PTS-STARTPTS[ca]")
        acur = "ca"
        if speed > 1.001:
            g.append(f"[ca]atempo={speed:.4f}[cas]")
            acur = "cas"
        if voice:
            g.append(f"[{acur}]highpass=f=70,loudnorm=I=-16:TP=-1.5:LRA=11[vo]")
        else:
            g.append(f"[{acur}]volume=0.30[vo]")
        mix = "vo"
        if music_idx is not None:
            mv = prof.music_vol if voice else 0.75
            g.append(f"[{music_idx}:a]atrim=0:{D + 0.5:.2f},asetpts=PTS-STARTPTS,volume={mv},"
                     f"afade=t=in:st=0:d=0.8,afade=t=out:st={max(D - 1.5, 0):.2f}:d=1.5[mu]")
            if voice:
                g.append("[vo]asplit=2[vo1][vo2]")
                g.append("[mu][vo2]sidechaincompress=threshold=0.03:ratio=10:attack=15:release=350[mud]")
                g.append("[vo1][mud]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mx]")
            else:
                g.append("[vo][mu]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mx]")
            mix = "mx"
        if sfx_idx:
            for k, (idx, tt) in enumerate(zip(sfx_idx, sfx_times)):
                ms = int(tt * 1000)
                g.append(f"[{idx}:a]volume=0.5,adelay={ms}:all=1[sx{k}]")
            g.append(f"[{mix}]" + "".join(f"[sx{k}]" for k in range(len(sfx_idx))) +
                     f"amix=inputs={len(sfx_idx) + 1}:duration=first:dropout_transition=0:normalize=0[mx2]")
            mix = "mx2"
        g.append(f"[{mix}]alimiter=limit=0.95,atrim=0:{D:.3f},asetpts=PTS-STARTPTS[aout]")

        fps = round(info["fps"]) if 24 <= info["fps"] <= 60 else 30
        out_path = c["out"] / prof.filename
        cmd += ["-filter_complex", ";".join(g), "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high",
                "-pix_fmt", "yuv420p", "-r", str(fps),
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-movflags", "+faststart", "-t", f"{D:.3f}", str(out_path)]
        run(cmd, cwd=str(work))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    srt_path = None
    if rw:
        srt_path = c["out"] / prof.filename.replace(".mp4", ".srt")
        srt_path.write_text(build_srt(rw), encoding="utf-8")

    qc = verify(out_path, prof, voice)
    zooms = sum(1 for p in pieces if p[2] > 0)
    decisions = [
        f"Duración final: {qc['duracion']} s (objetivo {prof.min_s:.0f}-{prof.target_max:.0f} s, máximo {prof.hard_max:.0f} s).",
        (f"Cortes: {len(segs)} tramos de voz; se eliminaron {dur - total:.1f} s de silencios, pausas y muletillas."
         if voice else f"Sin voz: se aceleró x{speed:.2f} para entrar en la duración." if speed > 1.001
         else "Sin voz: se conservó el ritmo original."),
        f"Encuadres: {len(pieces)} planos, {zooms} con punch-in del {int(prof.zoom_amp * 100)} %.",
        f"Color: {prof.grade.split(',')[0].split('=')[0]} + ajustes de contraste/saturación.",
        f"Música: {music.name}." if music else "Música: no se cargó biblioteca, se exportó sin música.",
        f"Efectos de sonido: {len(sfx_idx)}." if sfx_idx else "Efectos de sonido: ninguno.",
        "Subtítulos: " + ("con la palabra que se dice resaltada." if prof.highlight and voice
                          else "minimalistas." if voice else "textos en pantalla." if c["texts"] else "no corresponden (sin voz)."),
        "Logo: aplicado como marca de agua." if logo_idx is not None else "Logo: no se aplicó (sin kit de marca).",
    ]
    if prof.cta:
        decisions.append("Cierre con llamada a la acción y datos de contacto." if has_cta
                         else "Cierre con CTA: omitido porque el kit de marca no trae 'cta' ni 'contacto'.")
    return {"archivo": str(out_path), "srt": str(srt_path) if srt_path else None,
            "decisiones": decisions + notes, "calidad": qc}


# --------------------------------------------------------------------------
# Título, descripción y gancho (opcional, requiere ANTHROPIC_API_KEY)
# --------------------------------------------------------------------------
def generate_copy(transcript: str, brand: dict, hook: str | None, texts: list[str]) -> dict | None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    ctx = transcript or ("(video sin voz) Textos indicados: " + " | ".join(texts) if texts else "(video sin voz, sin texto)")
    prompt = (
        "Sos un editor de contenido para redes de Argentina. Con base en este video escribí, en español "
        "argentino, los textos para publicarlo. No inventes precios, resultados ni beneficios que no "
        "aparezcan en el material.\n\n"
        f"Marca: {brand.get('nombre', 'sin datos')}\nTranscripción / contexto:\n{ctx}\n\n"
        "Respondé SOLO un JSON con las claves: titulo, descripcion_instagram, gancho_alternativo, "
        "version_recomendada (viral, premium o comercial) y motivo (una frase).")
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(model="claude-sonnet-5-5", max_tokens=1200,
                                     messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if b.type == "text")
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as exc:  # no frenar la entrega por esto
        print(f"  (No se pudo generar el texto de publicación: {exc})")
        return None


# --------------------------------------------------------------------------
# Flujo principal
# --------------------------------------------------------------------------
def process_video(video, out_dir, brand=None, music_dir=None, sfx_dir=None, hook=None,
                  texts=None, reframe="crop", model="small", lang="es", words_json=None,
                  versions=None, log=print) -> dict:
    check_tools()
    video, out = Path(video).resolve(), Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    info = probe(video)
    brand_d = load_brand(brand)
    texts = [t.strip() for t in (texts or []) if t.strip()]

    words, voice = [], False
    if words_json:
        words = json.loads(Path(words_json).read_text(encoding="utf-8"))
        voice = len(words) > 0
    elif info["has_audio"]:
        log("1/3 Transcribiendo y detectando voz (Whisper)...")
        words = transcribe(video, model, lang)
        voice = len(words) >= 6
    if voice:
        (out / "palabras.json").write_text(json.dumps(words, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "transcripcion.txt").write_text(" ".join(w["w"] for w in words), encoding="utf-8")
        log(f"    Voz detectada: {len(words)} palabras.")
    else:
        log("    No se detectó voz: modo video sin voz.")

    ctx = {"video": video, "info": info, "voice": voice, "words": words, "brand": brand_d,
           "music_dir": str(Path(music_dir).resolve()) if music_dir else None,
           "sfx_dir": str(Path(sfx_dir).resolve()) if sfx_dir else None, "hook": hook, "texts": texts,
           "reframe": reframe, "out": out, "used_music": set(), "seed": video.stem}
    results = {}
    for n, key in enumerate(versions or PROFILES, 1):
        log(f"2/3 Renderizando versión {n}: {key}...")
        results[key] = render_version(PROFILES[key], ctx)

    log("3/3 Generando informe de entrega...")
    transcript = " ".join(w["w"] for w in words)
    copy = generate_copy(transcript, brand_d, hook, texts)

    lines = ["# Entrega\n"]
    for key, r in results.items():
        lines.append(f"## {PROFILES[key].filename}\n")
        lines += [f"- {d}" for d in r["decisiones"]]
        q = r["calidad"]
        lines.append("- Verificación: " + ("OK, sin problemas detectados." if q["ok"]
                                           else "REVISAR: " + " ".join(q["problemas"])))
        lines.append("")
    if copy:
        lines += ["## Texto para publicar\n",
                  f"**Título:** {copy.get('titulo', '')}\n",
                  f"**Descripción para Instagram:**\n{copy.get('descripcion_instagram', '')}\n",
                  f"**Gancho alternativo:** {copy.get('gancho_alternativo', '')}\n",
                  f"**Versión recomendada:** {copy.get('version_recomendada', '')}: {copy.get('motivo', '')}\n"]
    else:
        lines.append("_Título, descripción y gancho alternativo: no se generaron "
                     "(configurá ANTHROPIC_API_KEY e instalá `anthropic` para activarlos)._")
    (out / "entrega.md").write_text("\n".join(lines), encoding="utf-8")
    return {"carpeta": str(out), "voz": voice, "versiones": results, "texto_publicacion": copy}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Editor automático: 3 versiones 9:16 de un video.")
    ap.add_argument("video")
    ap.add_argument("--out", default="salida", help="carpeta de salida")
    ap.add_argument("--brand", help="brand.json con el kit de marca")
    ap.add_argument("--music", help="carpeta con música con licencia comercial")
    ap.add_argument("--sfx", help="carpeta con efectos de sonido")
    ap.add_argument("--hook", help="texto de gancho para los primeros 2,5 s (viral y comercial)")
    ap.add_argument("--texts", help="textos en pantalla para videos SIN voz, separados por |")
    ap.add_argument("--reframe", choices=["crop", "blur"], default="crop",
                    help="video horizontal: recortar al centro (crop) o fondo desenfocado (blur)")
    ap.add_argument("--model", default="small", help="modelo Whisper: tiny, base, small, medium, large-v3")
    ap.add_argument("--lang", default="es")
    ap.add_argument("--words-json", help="transcripción ya hecha (palabras.json) para no repetir Whisper")
    ap.add_argument("--only", nargs="+", choices=list(PROFILES), help="renderizar solo estas versiones")
    a = ap.parse_args()

    res = process_video(a.video, a.out, a.brand, a.music, a.sfx, a.hook,
                        a.texts.split("|") if a.texts else None, a.reframe, a.model, a.lang,
                        a.words_json, a.only)
    print("\nListo. Archivos en:", res["carpeta"])
    for key, r in res["versiones"].items():
        q = r["calidad"]
        estado = "OK" if q["ok"] else "REVISAR: " + " ".join(q["problemas"])
        print(f"  {PROFILES[key].filename}  ({q['duracion']} s)  {estado}")


if __name__ == "__main__":
    main()
