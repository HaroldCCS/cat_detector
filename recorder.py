"""Grabación de video por segmentos, con pregrabación, usando ffmpeg."""

import queue
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import cv2


def encoder_args(bitrate=None):
    if sys.platform == "darwin":
        # Codificador por hardware del Mac: casi no usa CPU.
        return ["-c:v", "h264_videotoolbox", "-b:v", str(bitrate or "3M")]
    if bitrate:
        return ["-c:v", "libx264", "-preset", "veryfast", "-b:v", str(bitrate)]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26"]


def build_compilation(paths, total_seconds, out_path, max_seconds=180, max_bytes=45_000_000, log=print):
    """Une varios videos en uno solo.

    Si la duración total pasa de `max_seconds`, lo acelera para que quepa, y
    ajusta el bitrate para no pasar de `max_bytes` (los bots de Telegram no
    pueden enviar más de 50 MB). Devuelve {"duration", "speed", "bytes"} o None.
    """
    out_path = Path(out_path)
    if not paths:
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    speed = max(1.0, total_seconds / max_seconds)
    out_seconds = max(total_seconds / speed, 1.0)
    bitrate = int(min(3_000_000, max_bytes * 8 * 0.85 / out_seconds))

    list_file = out_path.with_suffix(".txt")
    lines = []
    for p in paths:
        escaped = str(Path(p).resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'\n")
    list_file.write_text("".join(lines), encoding="utf-8")
    vf = f"setpts=PTS/{speed:.4f}" if speed > 1 else "null"
    try:
        for _ in range(2):  # si se pasa de peso, reintenta con menos bitrate
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-vf", vf, "-r", "30", *encoder_args(bitrate),
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(out_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            if proc.returncode != 0 or not out_path.exists():
                log(f"Recopilación: ffmpeg falló. {proc.stderr.strip()[-300:]}")
                return None
            size = out_path.stat().st_size
            if size <= max_bytes * 1.08:
                return {"duration": out_seconds, "speed": speed, "bytes": size}
            bitrate = int(bitrate * max_bytes / size * 0.85)
        log(f"Recopilación: quedó de {size / 1e6:.0f} MB, demasiado pesada para Telegram.")
        return None
    finally:
        list_file.unlink(missing_ok=True)


class VideoRecorder:
    """Graba segmentos de `segment_seconds` mientras haya gato.

    Recibe todos los frames de la cámara con push() (desde el hilo lector) y
    los procesa en su propio hilo:
    - Sin grabar: guarda los últimos `preroll_seconds` en memoria (JPEG).
    - Tras cat_seen(): empieza un segmento con esa pregrabación delante.
    - Al cumplir `segment_seconds`: cierra el archivo y, si el gato se vio en
      los últimos `continue_grace` segundos, empieza otro sin cortar.
    Cada segmento cerrado se entrega a on_segment(path, info).

    Recuadros: YOLO procesa ~1 de cada 3 frames del video. Con
    set_detections() se le pasa el último resultado y el grabador lo dibuja en
    todos los frames siguientes hasta que llegue otro (o pase `BOX_TTL`), así
    los recuadros se ven continuos aunque el video vaya a 30 FPS.
    """

    BOX_TTL = 0.5  # s que un recuadro sigue dibujado sin detección nueva
    BOX_COLOR = (255, 0, 255)  # magenta: se ve bien de noche y de día

    def __init__(self, out_dir, segment_seconds, preroll_seconds, max_side, on_segment, log,
                 continue_grace=3.0, draw_boxes=True, draw_clock=True):
        self.out_dir = Path(out_dir)
        self.segment_seconds = float(segment_seconds)
        self.preroll_seconds = float(preroll_seconds)
        self.max_side = int(max_side)
        self.on_segment = on_segment
        self.log = log
        self.continue_grace = continue_grace
        self.draw_boxes = draw_boxes
        self.draw_clock = draw_clock
        self.enabled = True  # False = no empezar ni continuar segmentos
        self._detections = (0.0, None, [])  # (momento, (w, h) del frame original, [(x1, y1, x2, y2, conf)])

        self._queue = queue.Queue(maxsize=120)
        self._preroll = deque()  # (t, jpeg)
        self._times = deque(maxlen=60)  # llegada de frames, para medir FPS
        self._size = None
        self._last_seen = None
        self._want_start = False
        self._proc = None
        self._seg = None
        self._count = 0
        self._dropped = 0
        self._running = False
        self._thread = None
        self._finalizers = []

    # --- API pública -------------------------------------------------------
    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Cierra el segmento en curso (se guarda, pero no se envía)."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        for t in self._finalizers:
            t.join(timeout=30)

    def push(self, frame):
        try:
            self._queue.put_nowait((time.monotonic(), frame))
        except queue.Full:
            self._dropped += 1

    def cat_seen(self):
        self._last_seen = time.monotonic()
        if self.enabled:
            self._want_start = True

    def set_detections(self, boxes, frame_size):
        """boxes: [(x1, y1, x2, y2, conf)] en píxeles del frame original (w, h)."""
        self._detections = (time.monotonic(), frame_size, boxes)

    @property
    def recording(self):
        return self._proc is not None

    # --- Hilo de grabación -------------------------------------------------
    def _loop(self):
        while self._running:
            try:
                t, frame = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._times.append(t)
            small = self._draw(self._resize(frame), frame)

            if self._proc is None:
                if self._want_start and self.enabled:
                    self._want_start = False
                    self._start_segment(preroll=True)
                else:
                    self._remember(t, small)
                    continue

            self._write(small)
            if self._proc is not None and t - self._seg["start"] >= self.segment_seconds:
                self._finish_segment()
                recent = self._last_seen is not None and t - self._last_seen <= self.continue_grace
                if self.enabled and recent:
                    self._start_segment(preroll=False)
                # Un cat_seen() viejo no debe arrancar otro segmento.
                self._want_start = False

        if self._proc is not None:
            self._finish_segment(final=True, wait=True)

    def _resize(self, frame):
        h, w = frame.shape[:2]
        if self._size is None:
            scale = min(1.0, self.max_side / max(w, h))
            self._size = (int(w * scale) // 2 * 2, int(h * scale) // 2 * 2)
        if (w, h) == self._size:
            return frame
        return cv2.resize(frame, self._size, interpolation=cv2.INTER_AREA)

    def _draw(self, img, original):
        stamp, src_size, boxes = self._detections
        show_boxes = self.draw_boxes and boxes and time.monotonic() - stamp <= self.BOX_TTL
        if not show_boxes and not self.draw_clock:
            return img
        if img is original:
            img = img.copy()  # el frame original lo comparte el detector
        if self.draw_clock:
            self._draw_clock(img)
        if not show_boxes:
            return img
        sx = img.shape[1] / src_size[0]
        sy = img.shape[0] / src_size[1]
        thick = max(2, img.shape[0] // 240)
        for x1, y1, x2, y2, conf in boxes:
            p1 = (int(x1 * sx), int(y1 * sy))
            p2 = (int(x2 * sx), int(y2 * sy))
            cv2.rectangle(img, p1, p2, self.BOX_COLOR, thick)
            label = f"gato {conf:.2f}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
            ty = max(p1[1], th + 8)
            cv2.rectangle(img, (p1[0], ty - th - 8), (p1[0] + tw + 8, ty), self.BOX_COLOR, -1)
            cv2.putText(img, label, (p1[0] + 4, ty - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
        return img

    @staticmethod
    def _draw_clock(img):
        """Fecha y hora abajo a la izquierda, para ubicarse en las recopilaciones."""
        text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        scale = img.shape[0] / 900
        thick = max(1, round(scale * 2))
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
        x, y = 10, img.shape[0] - 12
        cv2.rectangle(img, (x - 6, y - th - 8), (x + tw + 6, y + 6), (0, 0, 0), -1)
        cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), thick, cv2.LINE_AA)

    def _remember(self, t, img):
        if self.preroll_seconds <= 0:
            return
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            self._preroll.append((t, jpg))
        while self._preroll and t - self._preroll[0][0] > self.preroll_seconds:
            self._preroll.popleft()

    def _fps(self):
        if len(self._times) >= 10:
            span = self._times[-1] - self._times[0]
            if span > 0:
                return max(5.0, min(60.0, (len(self._times) - 1) / span))
        return 30.0

    def _start_segment(self, preroll):
        fps = self._fps()
        now = datetime.now()
        folder = self.out_dir / now.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"video_{now.strftime('%H-%M-%S_%f')[:-3]}.mp4"
        w, h = self._size
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps:.2f}", "-i", "-",
            *encoder_args(),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(path),
        ]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self._count += 1
        pre = list(self._preroll) if preroll else []
        self._preroll.clear()
        started_at = now - timedelta(seconds=(time.monotonic() - pre[0][0]) if pre else 0)
        self._seg = {
            "path": path,
            "start": time.monotonic(),
            "frames": 0,
            "fps": fps,
            "index": self._count,
            "started_at": started_at,
            "size": (w, h),
        }
        for _, jpg in pre:
            self._write(cv2.imdecode(jpg, cv2.IMREAD_COLOR))

    def _write(self, img):
        try:
            self._proc.stdin.write(img.tobytes())
            self._seg["frames"] += 1
        except (BrokenPipeError, OSError, ValueError):
            err = self._proc.stderr.read().decode(errors="ignore").strip() if self._proc.stderr else ""
            self.log(f"Video: ffmpeg se cerró inesperadamente. {err[-200:]}")
            self._proc.kill()
            self._proc = None
            self._seg = None

    def _finish_segment(self, final=False, wait=False):
        proc, seg = self._proc, self._seg
        self._proc, self._seg = None, None
        info = {
            "index": seg["index"],
            "duration": seg["frames"] / seg["fps"],
            "started_at": seg["started_at"],
            "size": seg["size"],
            "final": final,
        }

        def finalize():
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.log(f"Video: ffmpeg no terminó a tiempo con {seg['path'].name}.")
                return
            err = proc.stderr.read().decode(errors="ignore").strip()
            if proc.returncode != 0 or not seg["path"].exists():
                self.log(f"Video: no se pudo guardar {seg['path'].name}. {err[-200:]}")
                return
            self.log(f"Video guardado: {seg['path'].parent.name}/{seg['path'].name} ({info['duration']:.0f} s)")
            try:
                self.on_segment(seg["path"], info)
            except Exception as e:
                self.log(f"Video: error procesando el segmento: {e}")

        if wait:
            finalize()
        else:
            t = threading.Thread(target=finalize, daemon=True)
            t.start()
            self._finalizers = [f for f in self._finalizers if f.is_alive()] + [t]
