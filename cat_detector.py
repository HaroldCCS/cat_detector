#!/usr/bin/env python3
"""Detector de gatos sobre una cámara RTSP usando YOLO.

Modos:
  visual      -> muestra la cámara con las detecciones en tiempo real.
  vigilancia  -> solo terminal; abre la ventana cuando ve un gato y la cierra
                 tras N segundos sin verlo (window_keep_open_seconds).

Uso:
  .venv/bin/python cat_detector.py                 # menús interactivos
  .venv/bin/python cat_detector.py --camara 1 --modo vigilancia
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
MODELS_DIR = BASE_DIR / "modelos"
WINDOW = "Detector de gatos"

DEFAULT_CONFIG = {
    "cameras": [
        {"name": "Samsung", "url": "rtsp://192.168.1.9:1024/h264_pcm.sdp"},
    ],
    "default_camera": 0,
    "model": "yolo26n.pt",
    "device": "auto",
    "confidence": 0.4,
    "imgsz": 640,
    "window_keep_open_seconds": 20,
    "snapshot_interval_seconds": 2,
    "snapshot_with_boxes": True,
    "snapshots_dir": "capturas",
    "rtsp_transport": "tcp",
}

# Segundos sin detección antes de anunciar en la terminal que el gato se fue.
# Evita mensajes repetidos cuando la detección parpadea entre frames.
ABSENCE_GRACE_SECONDS = 2.0


# --------------------------------------------------------------------------- #
# Terminal
# --------------------------------------------------------------------------- #
_print_lock = threading.Lock()


def log(msg):
    """Imprime un evento en su propia línea (borra la línea de estado)."""
    stamp = datetime.now().strftime("%H:%M:%S")
    with _print_lock:
        sys.stdout.write(f"\r\033[K[{stamp}] {msg}\n")
        sys.stdout.flush()


def status(msg):
    """Reescribe la línea de estado actual sin saltar de línea."""
    with _print_lock:
        sys.stdout.write(f"\r\033[K{msg}")
        sys.stdout.flush()


def ask(prompt, default=""):
    try:
        answer = input(prompt).strip()
    except EOFError:
        answer = ""
    return answer or default


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
def load_config():
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    return {**DEFAULT_CONFIG, **cfg}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")


def resolve_device(device):
    if device != "auto":
        return device
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


# --------------------------------------------------------------------------- #
# Menús
# --------------------------------------------------------------------------- #
def add_camera(cfg):
    name = ask("  Nombre de la cámara: ")
    url = ask("  URL RTSP: ")
    if not url:
        print("  URL vacía, no se guardó nada.")
        return None
    cam = {"name": name or f"Cámara {len(cfg['cameras']) + 1}", "url": url}
    cfg["cameras"].append(cam)
    save_config(cfg)
    print(f"  Guardada en {CONFIG_PATH.name}.")
    return cam


def choose_camera(cfg):
    cams = cfg["cameras"]
    while True:
        if not cams:
            print("\nNo hay cámaras guardadas. Agrega una:")
            cam = add_camera(cfg)
            if cam:
                return cam
            continue

        default_idx = min(max(int(cfg.get("default_camera", 0)), 0), len(cams) - 1)
        print("\nCámaras guardadas:")
        for i, cam in enumerate(cams, 1):
            mark = "  (por defecto)" if i - 1 == default_idx else ""
            print(f"  {i}) {cam['name']:<12} {cam['url']}{mark}")
        print("  n) Agregar una cámara nueva")

        choice = ask(f"Elige cámara [Enter = {default_idx + 1}]: ", str(default_idx + 1)).lower()
        if choice == "n":
            cam = add_camera(cfg)
            if cam:
                return cam
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(cams):
            return cams[int(choice) - 1]
        print("Opción no válida.")


def choose_mode():
    print("\nModo:")
    print("  1) Visual      - ver la cámara y las detecciones en tiempo real")
    print("  2) Vigilancia  - solo terminal; abre la ventana al detectar un gato")
    while True:
        choice = ask("Elige modo [Enter = 1]: ", "1")
        if choice == "1":
            return "visual"
        if choice == "2":
            return "vigilancia"
        print("Opción no válida.")


# --------------------------------------------------------------------------- #
# Cámara
# --------------------------------------------------------------------------- #
class CameraStream:
    """Lee la cámara en un hilo aparte y guarda solo el frame más reciente.

    Así YOLO siempre procesa lo último que ve la cámara y no se acumula retraso
    aunque la inferencia sea más lenta que los FPS del stream. Si la conexión
    se cae, reintenta sola.
    """

    def __init__(self, url, transport="tcp"):
        self.url = url
        self.transport = transport
        self.connected = False
        self._lock = threading.Lock()
        self._frame = None
        self._frame_id = 0
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def read(self):
        with self._lock:
            return self._frame, self._frame_id

    def _open(self):
        opts = ["fflags;nobuffer", "flags;low_delay"]
        if self.transport:
            opts.insert(0, f"rtsp_transport;{self.transport}")
        # OpenCV lee esta variable cada vez que abre un stream con FFmpeg.
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(opts)
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _run(self):
        while self._running:
            cap = self._open()
            if not cap.isOpened():
                cap.release()
                log("No se pudo conectar a la cámara, reintento en 3 s...")
                time.sleep(3)
                continue

            log("Conectado a la cámara.")
            self.connected = True
            failures = 0
            while self._running:
                ok, frame = cap.read()
                if not ok:
                    failures += 1
                    if failures >= 10:
                        break
                    time.sleep(0.05)
                    continue
                failures = 0
                with self._lock:
                    self._frame = frame
                    self._frame_id += 1

            cap.release()
            self.connected = False
            if self._running:
                log("Se perdió la conexión con la cámara, reconectando...")
                time.sleep(2)


# --------------------------------------------------------------------------- #
# Ventana
# --------------------------------------------------------------------------- #
def open_window():
    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)


def close_window():
    cv2.destroyWindow(WINDOW)
    # En macOS la ventana no desaparece hasta procesar algunos eventos.
    for _ in range(5):
        cv2.waitKey(1)


def window_closed_by_user():
    try:
        return cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1
    except cv2.error:
        return True


def draw_overlay(img, lines):
    y = 28
    for text in lines:
        (w, h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        cv2.rectangle(img, (8, y - h - 8), (16 + w, y + 6), (0, 0, 0), -1)
        cv2.putText(img, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        y += h + 18


# --------------------------------------------------------------------------- #
# Detección
# --------------------------------------------------------------------------- #
def load_model(cfg):
    from ultralytics import YOLO

    model_name = cfg["model"]
    model_path = Path(model_name)
    if not model_path.is_absolute():
        MODELS_DIR.mkdir(exist_ok=True)
        model_path = MODELS_DIR / model_path.name
    model = YOLO(str(model_path))
    cat_ids = [i for i, name in model.names.items() if name == "cat"]
    if not cat_ids:
        sys.exit(f"El modelo {model_name} no tiene la clase 'cat'.")
    return model, cat_ids


def save_snapshot(img, snapshots_dir):
    now = datetime.now()
    folder = snapshots_dir / now.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"gato_{now.strftime('%H-%M-%S_%f')[:-3]}.jpg"
    cv2.imwrite(str(path), img)
    return path


def run(cfg, camera, mode):
    device = resolve_device(cfg["device"])
    keep_open = float(cfg["window_keep_open_seconds"])
    snap_interval = float(cfg["snapshot_interval_seconds"])
    snapshots_dir = BASE_DIR / cfg["snapshots_dir"]

    # La cámara conecta en su hilo mientras se carga y calienta el modelo.
    log(f"Conectando a {camera['name']} ({camera['url']})...")
    stream = CameraStream(camera["url"], cfg.get("rtsp_transport", "tcp"))
    stream.start()

    log(f"Cargando modelo {cfg['model']} en {device}...")
    model, cat_ids = load_model(cfg)
    # La primera inferencia es lenta (inicializa el backend); se hace aquí y
    # no con el primer frame real.
    model.predict(np.zeros((cfg["imgsz"], cfg["imgsz"], 3), np.uint8), device=device, verbose=False)
    log("Modelo listo.")

    window_open = False
    if mode == "visual":
        open_window()
        window_open = True

    last_fid = 0
    last_seen = None          # momento (monotonic) de la última detección
    last_snapshot = float("-inf")
    cat_present = False       # para avisar en terminal solo al cambiar
    snapshots = 0
    fps = 0.0
    infer_ms = 0.0
    last_status = 0.0
    prev_frame_t = None

    print("\nPresiona 'q' en la ventana o Ctrl+C en la terminal para salir.\n")

    try:
        while True:
            frame, fid = stream.read()
            if frame is None or fid == last_fid:
                if window_open:
                    if (cv2.waitKey(5) & 0xFF) == ord("q"):
                        break
                else:
                    time.sleep(0.005)
                if frame is None and time.monotonic() - last_status > 0.5:
                    status("Esperando video de la cámara...")
                    last_status = time.monotonic()
                continue
            last_fid = fid

            t0 = time.perf_counter()
            result = model.predict(
                frame,
                classes=cat_ids,
                conf=cfg["confidence"],
                imgsz=cfg["imgsz"],
                device=device,
                verbose=False,
            )[0]
            t1 = time.perf_counter()
            infer_ms = 0.9 * infer_ms + 0.1 * (t1 - t0) * 1000 if infer_ms else (t1 - t0) * 1000
            if prev_frame_t is not None:
                inst = 1.0 / max(t1 - prev_frame_t, 1e-6)
                fps = 0.9 * fps + 0.1 * inst if fps else inst
            prev_frame_t = t1

            now = time.monotonic()
            n_cats = len(result.boxes)
            annotated = None

            if n_cats:
                best = float(result.boxes.conf.max())
                last_seen = now
                if not cat_present:
                    cat_present = True
                    log(f"Gato detectado (x{n_cats}, confianza {best:.0%})")

                if now - last_snapshot >= snap_interval:
                    annotated = result.plot()
                    img = annotated if cfg["snapshot_with_boxes"] else frame
                    path = save_snapshot(img, snapshots_dir)
                    last_snapshot = now
                    snapshots += 1
                    log(f"Foto guardada: {path.relative_to(BASE_DIR)}")

                if not window_open:
                    open_window()
                    window_open = True
                    log("Ventana abierta.")
            elif cat_present and now - last_seen >= ABSENCE_GRACE_SECONDS:
                cat_present = False
                log("Ya no se ve el gato.")

            since_seen = (now - last_seen) if last_seen is not None else None

            if mode == "vigilancia" and window_open and not n_cats:
                if since_seen is None or since_seen >= keep_open:
                    close_window()
                    window_open = False
                    log(f"Ventana cerrada ({keep_open:g} s sin ver gato).")

            if window_open:
                if annotated is None:
                    annotated = result.plot()
                lines = [f"{camera['name']} | {mode} | {fps:.1f} FPS"]
                if n_cats:
                    lines.append(f"Gatos: {n_cats}")
                elif mode == "vigilancia" and since_seen is not None:
                    lines.append(f"Se cierra en {max(keep_open - since_seen, 0):.0f} s")
                draw_overlay(annotated, lines)
                cv2.imshow(WINDOW, annotated)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if window_closed_by_user():
                    if mode == "visual":
                        break
                    # En vigilancia, cerrar la ventana solo la oculta: se vuelve
                    # a abrir con la siguiente detección.
                    window_open = False
                    log("Ventana cerrada por el usuario.")

            if now - last_status > 0.5:
                state = f"GATO x{n_cats}" if n_cats else "sin gato"
                status(f"[{state}] {fps:5.1f} FPS | inferencia {infer_ms:4.0f} ms | fotos: {snapshots}")
                last_status = now
    except KeyboardInterrupt:
        pass
    finally:
        print()
        log(f"Saliendo. Fotos guardadas en esta sesión: {snapshots}")
        stream.stop()
        cv2.destroyAllWindows()
        for _ in range(5):
            cv2.waitKey(1)


def main():
    parser = argparse.ArgumentParser(description="Detector de gatos con YOLO sobre una cámara RTSP.")
    parser.add_argument("--camara", type=int, help="número de cámara de la lista (salta el menú)")
    parser.add_argument("--modo", choices=["visual", "vigilancia"], help="modo de ejecución (salta el menú)")
    args = parser.parse_args()

    cfg = load_config()
    print("=== Detector de gatos ===")

    if args.camara is not None:
        if not 1 <= args.camara <= len(cfg["cameras"]):
            sys.exit(f"--camara debe estar entre 1 y {len(cfg['cameras'])}.")
        camera = cfg["cameras"][args.camara - 1]
    else:
        camera = choose_camera(cfg)

    mode = args.modo or choose_mode()
    run(cfg, camera, mode)


if __name__ == "__main__":
    main()
