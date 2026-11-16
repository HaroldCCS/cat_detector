# Detector de gatos

Se conecta a una cámara RTSP (por ejemplo, el celular Samsung) y detecta gatos con YOLO (`yolo26n`, el modelo más pequeño).

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
| Telegram | Envía la foto a los suscriptores del bot | al detectar y luego cada 30 s como máximo |

Mientras el gato siga a la vista, las alertas se repiten con esos intervalos. No importa cuántas detecciones haya entre medio.

### Sirena

El sonido se genera solo en `sonidos/sirena.wav` la primera vez. Para usar otro sonido, pon su ruta en `siren_sound`. En macOS se reproduce con `afplay`; en Linux, con `paplay` o `aplay`.

### Telegram

1. Crea un bot con [@BotFather](https://t.me/BotFather) y copia el token.
2. Copia `.env.example` como `.env` y llénalo:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CLAVE=una-clave-larga-que-solo-sepan-los-tuyos
   ```
   `.env` no se sube al repo.
3. Arranca el detector. Cada persona que quiera alertas le escribe al bot:

| Comando | Qué hace |
|---|---|
| `/suscribir <clave>` | Empieza a recibir las fotos |
| `/salir` | Deja de recibirlas |
| `/estado` | Muestra si el detector está activo y cuántos suscriptores hay |

El bot solo atiende comandos mientras el detector está corriendo. Los mensajes enviados con el detector apagado se procesan al arrancarlo.

Los suscriptores se guardan en `suscriptores.json` (no se sube al repo). Si alguien bloquea el bot, se elimina solo de la lista.

## Configuración (`config.json`)

| Clave | Qué hace | Valor por defecto |
|---|---|---|
| `cameras` | Lista de cámaras `{name, url}` | Samsung |
| `default_camera` | Índice (desde 0) de la cámara por defecto | `0` |
| `window_keep_open_seconds` | Segundos que la ventana sigue abierta sin ver gato (modo vigilancia) | `20` |
| `snapshot_interval_seconds` | Mínimo de segundos entre fotos | `2` |
| `snapshot_with_boxes` | Guardar la foto con los recuadros dibujados | `true` |
| `confidence` | Confianza mínima para contar como gato | `0.4` |
| `model` | Modelo YOLO (`yolo26n.pt`, `yolo11n.pt`, `yolo26s.pt`...) | `yolo26n.pt` |
| `device` | `auto`, `mps`, `cpu` o `cuda` | `auto` |
| `imgsz` | Tamaño de entrada del modelo | `640` |
| `rtsp_transport` | `tcp` o `udp` | `tcp` |
| `snapshots_dir` | Carpeta de fotos | `capturas` |
| `siren_enabled` | Activar la sirena | `true` |
| `siren_sound` | Ruta a un sonido propio (vacío = sirena generada) | `""` |
| `siren_cooldown_seconds` | Mínimo de segundos entre sirenas | `15` |
| `telegram_enabled` | Activar el envío por Telegram | `true` |
| `telegram_interval_seconds` | Mínimo de segundos entre fotos enviadas | `30` |

Si la IP del celular cambia (pasa al reconectarse al wifi), edita la URL en `config.json` o agrega la nueva desde el menú.
