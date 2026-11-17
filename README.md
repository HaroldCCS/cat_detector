# Detector de gatos

Se conecta a una cámara RTSP (por ejemplo, el celular Samsung) y detecta gatos con YOLO (`yolo26l` a 960 px).

### ¿Por qué `yolo26l`?

Medido sobre 57 fotos reales de gatos de esta cámara, en un Mac con Apple Silicon (MPS):

| Modelo | `imgsz` | Fotos con gato detectado (conf ≥ 0.40) | ms por frame |
|---|---|---|---|
| `yolo26n` | 640 | 38/57 | 21 |
| `yolo26s` | 960 | 48/57 | 41 |
| `yolo26m` | 960 | 52/57 | 72 |
| `yolo26l` | 960 | 55/57 | 89 |

El nano se pierde un tercio de los gatos, sobre todo los lejanos, los negros y los que están en posturas raras. El `l` a 960 px encuentra casi todos y sigue corriendo a ~11 FPS, que es de sobra para vigilar. Si tu equipo es más lento, `yolo26m` es un buen punto medio.

## Instalación (una sola vez)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

El modelo se descarga solo en `modelos/` la primera vez que se ejecuta.

## Uso

```bash
.venv/bin/python cat_detector.py
```

Aparecen dos menús:

1. **Cámara**: elige una de la lista guardada (Enter = la de por defecto) o `n` para agregar una nueva. Las nuevas quedan guardadas en `config.json`.
2. **Modo**:
   - **Visual**: ventana con la cámara y las detecciones en tiempo real.
   - **Vigilancia**: solo terminal. Al detectar un gato abre la ventana y la cierra tras 20 s sin verlo.

Para saltar los menús:

```bash
.venv/bin/python cat_detector.py --camara 1 --modo vigilancia
```

Salir: `q` en la ventana o `Ctrl+C` en la terminal.

## Alertas al detectar un gato

Todas funcionan en los dos modos y cada una respeta su propio intervalo:

| Alerta | Qué hace | Intervalo por defecto |
|---|---|---|
| Foto local | Guarda la foto en `capturas/AAAA-MM-DD/` | cada 2 s |
| Sirena | Suena un sonido de sirena de 4 s | cada 15 s como máximo |
| Video | Graba videos de 10 s en `capturas/AAAA-MM-DD/` | seguidos mientras haya gato |
| Telegram | Envía las fotos y los videos a los suscriptores del bot | fotos cada 2 s, cada video al cerrarse |

Mientras el gato siga a la vista, las alertas se repiten con esos intervalos. No importa cuántas detecciones haya entre medio.

### Sirena

El sonido se genera solo en `sonidos/sirena.wav` la primera vez. Para usar otro sonido, pon su ruta en `siren_sound`. En macOS se reproduce con `afplay`; en Linux, con `paplay` o `aplay`.

### Video

Necesita `ffmpeg` instalado (`brew install ffmpeg`).

- Al detectar un gato empieza a grabar un video de 10 s. Lleva delante los 2 s anteriores a la detección, para que se vea al gato llegar.
- Al cumplirse los 10 s el video se cierra y se envía por Telegram. Si el gato se vio en los últimos 3 s, empieza otro sin cortar.
- Se graba a la velocidad de la cámara (~30 FPS), en 720p y H.264.
- YOLO solo procesa ~1 de cada 3 frames. Cada recuadro se dibuja en los frames siguientes hasta la próxima detección, así se ven continuos y ayudan a ubicar al gato cuando está oscuro.
- Cada frame lleva la fecha y la hora abajo a la izquierda.
- No lleva audio.

### Recopilación al final de la visita

Cuando el gato se va (30 s sin verlo) y la visita dejó 2 videos o más, cada suscriptor recibe:

> El gato se fue tras 4 min (24 videos). ¿Quieres una recopilación de los videos de esta visita? **[Sí] [No]**

- Con **Sí**, se unen todos los videos de la visita en uno solo y se envía. También entran los que no se enviaron por estar en "menos seguido" o esperando respuesta.
- La recopilación se genera una sola vez, aunque varios digan que sí, y se guarda como `capturas/AAAA-MM-DD/recopilacion_HH-MM-SS.mp4`.
- Los bots de Telegram no pueden enviar videos de más de 50 MB. Si la visita fue larga, la recopilación se acelera para durar como máximo 3 min (`compilation_max_seconds`) y se comprime para pesar menos de 45 MB. Por ejemplo, 30 min de gato quedan en 3 min a x10.
- Si el detector se reinicia, los botones de visitas anteriores dejan de funcionar. Los videos siguen en disco.

### Visitas largas

Si el gato lleva más de 1 minuto, el bot le pregunta al **administrador** (el primero de `suscriptores.json`) qué hacer, con tres botones:

| Botón | Qué pasa |
|---|---|
| Seguir igual | Fotos cada 2 s y cada video. Si el gato sigue ahí, vuelve a preguntar en 10 min. |
| Menos seguido | Una foto cada 30 s y un video por minuto. Vuelve a preguntar en 10 min. |
| Dejar de grabar | No graba ni envía nada hasta que el gato se vaya. |

Mientras el administrador no responde, se sigue grabando en disco pero no se envía nada a nadie. Cuando el gato lleva 30 s sin verse, la visita termina, el administrador recibe un aviso y todo vuelve a lo normal. La sirena no cambia con estas opciones.

### Telegram

1. Crea un bot con [@BotFather](https://t.me/BotFather) y copia el token.
2. Copia `.env.example` como `.env` y pon el token:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   ```
   `.env` no se sube al repo.
3. Arranca el detector. Cada persona que quiera alertas le escribe al bot:

| Comando | Qué hace |
|---|---|
| `/suscribir` | Empieza a recibir las fotos y los videos cuando haya un gato |
| `/foto` | Envía lo que la cámara está viendo en este momento, con recuadros si hay un gato (solo para suscritos) |
| `/estado` | Muestra si el detector está activo y cuántos suscriptores hay |
| `/salir` | Deja de recibir alertas |

Al arrancar, el detector registra estos comandos en el menú del bot, así aparecen al escribir `/` en Telegram.

El bot solo atiende comandos mientras el detector está corriendo. Los mensajes enviados con el detector apagado se procesan al arrancarlo. Solo puede correr un detector a la vez con el mismo bot.

Los suscriptores se guardan en `suscriptores.json` (no se sube al repo). Si alguien bloquea el bot, se elimina solo de la lista. Para quitar a alguien a mano, edita ese archivo con el detector apagado.

> **Ojo:** cualquiera que encuentre el bot en Telegram puede suscribirse y pedir fotos con `/foto`. Cada suscripción nueva y cada `/foto` quedan registradas en la terminal con el nombre y el ID de quien lo hizo.

## Configuración (`config.json`)

| Clave | Qué hace | Valor por defecto |
|---|---|---|
| `cameras` | Lista de cámaras `{name, url}` | Samsung |
| `default_camera` | Índice (desde 0) de la cámara por defecto | `0` |
| `window_keep_open_seconds` | Segundos que la ventana sigue abierta sin ver gato (modo vigilancia) | `20` |
| `snapshot_interval_seconds` | Mínimo de segundos entre fotos | `2` |
| `snapshot_with_boxes` | Guardar la foto con los recuadros dibujados | `true` |
| `confidence` | Confianza mínima para contar como gato | `0.4` |
| `model` | Modelo YOLO (`yolo26n.pt`, `yolo26s.pt`, `yolo26m.pt`, `yolo26l.pt`, `yolo26x.pt`) | `yolo26l.pt` |
| `device` | `auto`, `mps`, `cpu` o `cuda` | `auto` |
| `imgsz` | Tamaño de entrada del modelo; más grande ayuda con gatos lejanos | `960` |
| `rtsp_transport` | `tcp` o `udp` | `tcp` |
| `snapshots_dir` | Carpeta de fotos | `capturas` |
| `siren_enabled` | Activar la sirena | `true` |
| `siren_sound` | Ruta a un sonido propio (vacío = sirena generada) | `""` |
| `siren_cooldown_seconds` | Mínimo de segundos entre sirenas | `15` |
| `telegram_enabled` | Activar el envío por Telegram | `true` |
| `telegram_interval_seconds` | Mínimo de segundos entre fotos enviadas | `2` |
| `video_enabled` | Activar la grabación de video | `true` |
| `video_segment_seconds` | Duración de cada video | `10` |
| `video_preroll_seconds` | Segundos antes de la detección que se agregan al primer video | `2` |
| `video_max_side` | Lado más largo del video en píxeles (1280 = 720p) | `1280` |
| `video_with_boxes` | Dibujar los recuadros de YOLO en el video | `true` |
| `video_with_clock` | Dibujar la fecha y la hora en el video | `true` |
| `compilation_max_seconds` | Duración máxima de la recopilación; si la visita fue más larga, se acelera | `180` |
| `long_visit_ask_after_seconds` | Tras cuántos segundos de visita se le pregunta al administrador | `60` |
| `long_visit_reask_minutes` | Cada cuánto vuelve a preguntar si eligió seguir | `10` |
| `reduced_photo_interval_seconds` | Intervalo de fotos en modo "menos seguido" | `30` |
| `reduced_video_interval_seconds` | Intervalo de videos en modo "menos seguido" | `60` |

Si la IP del celular cambia (pasa al reconectarse al wifi), edita la URL en `config.json` o agrega la nueva desde el menú.
