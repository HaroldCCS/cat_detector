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

## Fotos

Mientras se vea un gato, se guarda una foto cada 2 s en `capturas/AAAA-MM-DD/`. No importa cuántas detecciones haya entre medio.

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

Si la IP del celular cambia (pasa al reconectarse al wifi), edita la URL en `config.json` o agrega la nueva desde el menú.
