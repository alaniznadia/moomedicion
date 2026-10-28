# Editor automático de video (3 versiones)

## Instalación (una sola vez)
1. Instalá Python 3.10 o superior.
2. Instalá FFmpeg y comprobá que `ffmpeg -version` funcione en la terminal.
   - Windows: build "full" de gyan.dev, o `winget install ffmpeg`
   - Mac: `brew install ffmpeg`
3. `pip install -r requirements.txt`
   (`anthropic` es opcional: sirve para generar título, descripción y gancho.
   Para activarlo, definí la variable de entorno ANTHROPIC_API_KEY.)

## Uso
    python editor_video.py mi_video.mp4
    python editor_video.py mi_video.mp4 --brand brand.json --music musica --sfx sfx

Videos sin voz (producto, proceso, antes y después):
    python editor_video.py proceso.mp4 --texts "Antes|Proceso|Resultado final" --hook "Mirá este cambio"

Video horizontal con fondo desenfocado en vez de recorte al centro:
    python editor_video.py mi_video.mp4 --reframe blur

## Carpetas opcionales
- `musica/`: pistas con licencia comercial. Podés crear subcarpetas
  `viral/`, `premium/` y `comercial/` para una música distinta por versión.
- `sfx/`: efectos cortos (whoosh, pop). Se usan en viral y comercial.
- `brand.json`: copiá `brand.example.json` y completalo. El logo debe ser PNG.
  Si usás una fuente propia, ponela en la carpeta `fonts` junto al brand.json.

## Qué genera (carpeta `salida/`)
VIDEO_VIRAL.mp4, VIDEO_PREMIUM.mp4, VIDEO_COMERCIAL.mp4, los .srt,
`entrega.md` (decisiones de edición, verificación de calidad y textos de publicación),
`palabras.json` y `transcripcion.txt`.

## Límites conocidos
- El reencuadre horizontal a vertical es al centro (o con fondo desenfocado); no sigue caras.
- No estabiliza imagen ni elige música por su cuenta: usa solo tu biblioteca.
- Los zooms son cambios de encuadre (punch-in) entre planos, no movimientos animados.
- Si repetís un video, usá `--words-json salida/palabras.json` para no volver a transcribir.
