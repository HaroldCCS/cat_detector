"""Alertas del detector: sirena, fotos y videos por Telegram, visitas largas."""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests

from recorder import VideoRecorder, build_compilation


# --------------------------------------------------------------------------- #
# Sirena
# --------------------------------------------------------------------------- #
def generate_siren(path, duration=4.0, sample_rate=44100):
    """Sintetiza una sirena que sube y baja de tono y la guarda como WAV."""
    t = np.arange(int(duration * sample_rate)) / sample_rate
    freq = 1050 + 450 * np.sin(2 * np.pi * 0.7 * t)  # oscila entre 600 y 1500 Hz
    phase = 2 * np.pi * np.cumsum(freq) / sample_rate
    signal = np.sin(phase) + 0.35 * np.sin(3 * phase) + 0.15 * np.sin(5 * phase)
    signal /= np.abs(signal).max()

    fade = int(0.02 * sample_rate)
    envelope = np.ones_like(signal)
    envelope[:fade] = np.linspace(0, 1, fade)
    envelope[-fade:] = np.linspace(1, 0, fade)

    data = (signal * envelope * 0.8 * 32767).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(data.tobytes())


class Siren:
    """Reproduce un sonido sin bloquear, como máximo una vez cada `cooldown` s."""

    def __init__(self, sound_path, cooldown):
        self.path = Path(sound_path)
        self.cooldown = cooldown
        self._proc = None
        self._last = float("-inf")
        self._cmd = self._find_player()

    @staticmethod
    def _find_player():
        if sys.platform == "darwin":
            return ["afplay"]
        if sys.platform == "win32":
            return []  # se usa winsound
        for cmd in (["paplay"], ["aplay", "-q"]):
            if shutil.which(cmd[0]):
                return cmd
        return None

    @property
    def available(self):
        return self._cmd is not None and self.path.exists()

    def play(self):
        """Devuelve True si empezó a sonar."""
        now = time.monotonic()
        if not self.available or now - self._last < self.cooldown:
            return False
        if self._proc and self._proc.poll() is None:
            return False  # todavía está sonando
        self._last = now
        if sys.platform == "win32":
            import winsound

            winsound.PlaySound(str(self.path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            self._proc = subprocess.Popen(
                self._cmd + [str(self.path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return True

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
class TelegramError(Exception):
    def __init__(self, code, description, retry_after=None):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description or ""
        self.retry_after = retry_after

    @property
    def chat_gone(self):
        """El usuario bloqueó el bot o el chat ya no existe."""
        return self.code == 403 or (self.code == 400 and "chat not found" in self.description.lower())


COMMANDS = [
    ("suscribir", "Recibir la foto cuando vea un gato"),
    ("foto", "Ver lo que está viendo la cámara ahora"),
    ("estado", "Ver si el detector está activo"),
    ("salir", "Dejar de recibir alertas"),
]

HELP_TEXT = "Hola, soy el detector de gatos.\n\n" + "\n".join(f"/{c} - {d}" for c, d in COMMANDS)


class TelegramNotifier:
    """Envía fotos y videos a los suscriptores y atiende el bot.

    - Un hilo hace long polling de getUpdates: comandos y botones.
    - Dos hilos envían (uno fotos, otro videos), así un video que tarda en
      subir no retrasa las fotos y la detección nunca espera a la red.
    - Cada archivo se sube una sola vez; a los demás suscriptores se les
      reenvía con el file_id que devuelve Telegram.
    Los suscriptores se guardan en un JSON local (no se sube al repo). El
    primero de la lista es el administrador.

    `snapshot_provider()` devuelve (imagen, n_gatos, momento) para /foto.
    `on_choice(from_id, chat_id, message_id, data)` atiende los botones y
    devuelve (texto_breve, texto_a_añadir_al_mensaje).
    """

    def __init__(self, token, subscribers_path, camera_name, log, snapshot_provider=None, on_choice=None):
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}/"
        self.path = Path(subscribers_path)
        self.camera_name = camera_name
        self.log = log
        self.snapshot_provider = snapshot_provider
        self.on_choice = on_choice
        self.bot_name = None
        self._lock = threading.Lock()
        self._subs = self._load()
        self._photo_queue = queue.Queue(maxsize=3)
        self._video_queue = queue.Queue(maxsize=5)
        self._last_photo = float("-inf")
        self._running = False
        self._workers = []
        self._started_at = datetime.now()

    # --- API ---------------------------------------------------------------
    def _safe(self, err):
        """Texto del error sin el token (las URLs de requests lo incluyen)."""
        return str(err).replace(self._token, "***")

    def _call(self, method, timeout=20, **kwargs):
        resp = requests.post(self._base + method, timeout=timeout, **kwargs)
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(resp.status_code, "respuesta no válida")
        if not data.get("ok"):
            params = data.get("parameters") or {}
            raise TelegramError(data.get("error_code"), data.get("description"), params.get("retry_after"))
        return data["result"]

    def _reply(self, chat_id, text):
        try:
            self._call("sendMessage", data={"chat_id": chat_id, "text": text})
        except (TelegramError, requests.RequestException) as e:
            self.log(f"Telegram: no se pudo responder a {chat_id}: {self._safe(e)}")

    def send_text(self, chat_id, text):
        self._reply(chat_id, text)

    def ask(self, chat_id, text, options, per_row=1):
        """Envía un mensaje con botones [(texto, dato)]. Devuelve el message_id o None."""
        buttons = [{"text": label, "callback_data": data} for label, data in options]
        markup = {"inline_keyboard": [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]}
        try:
            result = self._call("sendMessage", data={"chat_id": chat_id, "text": text, "reply_markup": json.dumps(markup)})
            return result["message_id"]
        except (TelegramError, requests.RequestException) as e:
            self.log(f"Telegram: no se pudo enviar la pregunta a {chat_id}: {self._safe(e)}")
            return None

    def edit(self, chat_id, message_id, text):
        """Cambia el texto de un mensaje y le quita los botones."""
        try:
            self._call("editMessageText", data={"chat_id": chat_id, "message_id": message_id, "text": text})
        except (TelegramError, requests.RequestException) as e:
            self.log(f"Telegram: no se pudo editar el mensaje: {self._safe(e)}")

    # --- Suscriptores ------------------------------------------------------
    def _load(self):
        if not self.path.exists():
            return {}
        with open(self.path, encoding="utf-8") as f:
            items = json.load(f)
        return {int(s["chat_id"]): s for s in items}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(self._subs.values()), f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, self.path)

    def _add(self, chat_id, name):
        with self._lock:
            if chat_id in self._subs:
                return False
            self._subs[chat_id] = {
                "chat_id": chat_id,
                "nombre": name,
                "desde": datetime.now().isoformat(timespec="seconds"),
            }
            self._save()
            return True

    def _remove(self, chat_id):
        with self._lock:
            if self._subs.pop(chat_id, None) is None:
                return False
            self._save()
            return True

    @property
    def subscriber_count(self):
        with self._lock:
            return len(self._subs)

    @property
    def admin_chat_id(self):
        """El primer suscriptor de la lista."""
        with self._lock:
            return next(iter(self._subs), None)

    @property
    def subscriber_ids(self):
        with self._lock:
            return list(self._subs)

    # --- Ciclo de vida -----------------------------------------------------
    def start(self):
        """Valida el token y arranca los hilos. Lanza excepción si falla."""
        try:
            self.bot_name = self._call("getMe", timeout=10)["username"]
        except requests.RequestException as e:
            raise RuntimeError(self._safe(e)) from None
        try:
            # Menú de comandos que Telegram muestra al escribir "/".
            commands = [{"command": c, "description": d} for c, d in COMMANDS]
            self._call("setMyCommands", timeout=10, data={"commands": json.dumps(commands)})
        except (TelegramError, requests.RequestException) as e:
            self.log(f"Telegram: no se pudo registrar el menú de comandos: {self._safe(e)}")
        self._running = True
        for q in (self._photo_queue, self._video_queue):
            t = threading.Thread(target=self._send_loop, args=(q,), daemon=True)
            t.start()
            self._workers.append(t)
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def stop(self):
        self._running = False
        for t in self._workers:
            t.join(timeout=30)  # deja salir lo que ya estaba en cola

    # --- Envío -------------------------------------------------------------
    def notify(self, img, caption, min_interval):
        """Encola una foto si ya pasó `min_interval`. Devuelve True si la encoló."""
        now = time.monotonic()
        if now - self._last_photo < min_interval or not self.subscriber_count:
            return False
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return False
        item = ("sendPhoto", "photo", "gato.jpg", buf.tobytes(), "image/jpeg", caption, {}, False)
        try:
            self._photo_queue.put_nowait(item)
        except queue.Full:
            return False
        self._last_photo = now
        return True

    def send_video(self, path, caption, info):
        if not self.subscriber_count:
            return False
        w, h = info["size"]
        extra = {
            "supports_streaming": "true",
            "duration": int(round(info["duration"])),
            "width": w,
            "height": h,
        }
        item = ("sendVideo", "video", Path(path).name, Path(path).read_bytes(), "video/mp4", caption, extra, True)
        try:
            self._video_queue.put_nowait(item)
        except queue.Full:
            self.log(f"Telegram: cola de videos llena, no se envía {Path(path).name}.")
            return False
        return True

    def send_video_to(self, chat_id, path, caption, size, duration, file_id=None):
        """Envía un video a un solo chat. Devuelve el file_id para reutilizarlo."""
        w, h = size
        data = {
            "chat_id": chat_id,
            "caption": caption,
            "supports_streaming": "true",
            "duration": int(round(duration)),
            "width": w,
            "height": h,
        }
        try:
            if file_id:
                result = self._call("sendVideo", timeout=60, data={**data, "video": file_id})
            else:
                with open(path, "rb") as f:
                    result = self._call("sendVideo", timeout=300, data=data,
                                        files={"video": (Path(path).name, f, "video/mp4")})
            return self._file_id(result, "video")
        except (TelegramError, requests.RequestException) as e:
            self.log(f"Telegram: no se pudo enviar el video a {chat_id}: {self._safe(e)}")
            return None

    def _send_loop(self, q):
        while self._running or not q.empty():
            try:
                method, field, filename, content, mime, caption, extra, verbose = q.get(timeout=0.5)
            except queue.Empty:
                continue
            sent, total = self._send_to_all(method, field, filename, content, mime, caption, extra)
            if verbose:
                self.log(f"Telegram: video enviado a {sent}/{total} suscriptor(es).")

    def _send_to_all(self, method, field, filename, content, mime, caption, extra):
        with self._lock:
            chat_ids = list(self._subs)
        file_id = None
        sent = 0
        for chat_id in chat_ids:
            for attempt in (1, 2):
                data = {"chat_id": chat_id, "caption": caption, **extra}
                try:
                    if file_id:
                        result = self._call(method, timeout=60, data={**data, field: file_id})
                    else:
                        result = self._call(method, timeout=180, data=data, files={field: (filename, content, mime)})
                        file_id = self._file_id(result, field)
                    sent += 1
                    break
                except TelegramError as e:
                    if e.retry_after and attempt == 1:  # 429: demasiados mensajes
                        time.sleep(min(e.retry_after, 30))
                        continue
                    if e.chat_gone:
                        self._remove(chat_id)
                        self.log(f"Telegram: {chat_id} eliminado de suscriptores ({e}).")
                    else:
                        self.log(f"Telegram: error enviando a {chat_id}: {e}")
                    break
                except requests.RequestException as e:
                    self.log(f"Telegram: error de red enviando a {chat_id}: {self._safe(e)}")
                    break
        return sent, len(chat_ids)

    @staticmethod
    def _file_id(result, field):
        media = result.get(field)
        if isinstance(media, list):  # las fotos vienen en varios tamaños
            media = media[-1] if media else None
        return media.get("file_id") if media else None

    # --- Comandos y botones ------------------------------------------------
    def _poll_loop(self):
        offset = None
        network_ok = True
        while self._running:
            data = {"timeout": 25, "allowed_updates": json.dumps(["message", "callback_query"])}
            if offset is not None:
                data["offset"] = offset
            try:
                updates = self._call("getUpdates", timeout=35, data=data)
                if not network_ok:
                    self.log("Telegram: conexión recuperada.")
                    network_ok = True
            except (TelegramError, requests.RequestException) as e:
                if network_ok:
                    self.log(f"Telegram: error leyendo comandos: {self._safe(e)}")
                    network_ok = False
                time.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    if "callback_query" in update:
                        self._handle_callback(update["callback_query"])
                    elif (update.get("message") or {}).get("text"):
                        self._handle(update["message"])
                except Exception as e:  # un mensaje raro no debe tumbar el hilo
                    self.log(f"Telegram: error procesando mensaje: {self._safe(e)}")

    def _handle_callback(self, cq):
        msg = cq.get("message") or {}
        toast, suffix = "Esta pregunta ya no está activa.", "(Pregunta vencida)"
        if self.on_choice:
            chat_id = (msg.get("chat") or {}).get("id", cq["from"]["id"])
            toast, suffix = self.on_choice(cq["from"]["id"], chat_id, msg.get("message_id"), cq.get("data", ""))
        try:
            self._call("answerCallbackQuery", data={"callback_query_id": cq["id"], "text": toast or ""})
        except (TelegramError, requests.RequestException):
            pass
        if suffix and msg:
            self.edit(msg["chat"]["id"], msg["message_id"], f"{msg.get('text', '')}\n\n{suffix}")

    def _handle(self, msg):
        chat = msg["chat"]
        chat_id = chat["id"]
        name = (
            chat.get("title")
            or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            or chat.get("username")
            or str(chat_id)
        )
        command = msg["text"].strip().split(" ", 1)[0]
        command = command.split("@")[0].lower()  # /suscribir@MiBot en grupos

        if command in ("/start", "/ayuda", "/help"):
            self._reply(chat_id, HELP_TEXT)

        elif command == "/suscribir":
            if self._add(chat_id, name):
                self._reply(chat_id, "Listo, te enviaré la foto cuando vea un gato.\nUsa /foto para ver la cámara ahora.")
                self.log(f"Telegram: nuevo suscriptor {name} ({chat_id}).")
            else:
                self._reply(chat_id, "Ya estabas suscrito.")

        elif command == "/foto":
            if chat_id not in self._subs:
                self._reply(chat_id, "Primero envía /suscribir.")
                return
            self.log(f"Telegram: {name} ({chat_id}) pidió una foto.")
            self._send_current_photo(chat_id)

        elif command == "/salir":
            if self._remove(chat_id):
                self._reply(chat_id, "Ya no recibirás alertas.")
                self.log(f"Telegram: {name} ({chat_id}) se desuscribió.")
            else:
                self._reply(chat_id, "No estabas suscrito.")

        elif command == "/estado":
            since = self._started_at.strftime("%Y-%m-%d %H:%M")
            subscribed = "sí" if chat_id in self._subs else "no"
            self._reply(
                chat_id,
                f"Detector activo en {self.camera_name} desde {since}.\n"
                f"Suscriptores: {self.subscriber_count}. Tú suscrito: {subscribed}.",
            )

    def _send_current_photo(self, chat_id):
        snapshot = self.snapshot_provider() if self.snapshot_provider else None
        if snapshot is None:
            self._reply(chat_id, "La cámara todavía no está enviando video.")
            return
        img, n_cats, taken_at = snapshot
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            self._reply(chat_id, "No se pudo generar la foto.")
            return
        cats = f"{n_cats} gato(s) a la vista" if n_cats else "sin gatos a la vista"
        caption = f"{self.camera_name} · {taken_at:%Y-%m-%d %H:%M:%S} · {cats}"
        age = (datetime.now() - taken_at).total_seconds()
        if age > 10:
            caption += f"\nOjo: es el último frame recibido, de hace {age:.0f} s (la cámara no está enviando video)."
        try:
            self._call(
                "sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": ("camara.jpg", buf.tobytes(), "image/jpeg")},
            )
        except (TelegramError, requests.RequestException) as e:
            self.log(f"Telegram: no se pudo enviar /foto a {chat_id}: {self._safe(e)}")


# --------------------------------------------------------------------------- #
# Visitas
# --------------------------------------------------------------------------- #
# Sin ver al gato durante este tiempo, la visita se da por terminada.
VISIT_END_SECONDS = 30


def fmt_duration(seconds):
    minutes = int(seconds // 60)
    if minutes < 1:
        return f"{int(seconds)} s"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


class Visit:
    """Una visita del gato: desde que aparece hasta que lleva un rato sin verse.

    mode:
      normal   -> fotos y videos con los intervalos normales
      waiting  -> se le preguntó al administrador; se graba pero no se envía
      reduced  -> fotos y videos menos seguido
      stopped  -> no se graba ni se envía hasta que termine la visita
    """

    _counter = 0

    def __init__(self, now, ask_at):
        Visit._counter += 1
        self.id = Visit._counter
        self.started = now
        self.started_at = datetime.now()
        self.segments = []  # [(path, info)] de todos los videos grabados en la visita
        self.last_seen = now
        self.mode = "normal"
        self.videos = 0
        self.last_video_sent = float("-inf")
        self.next_question_at = ask_at
        self.question = None  # (chat_id, message_id, texto) mientras espera respuesta
        self.asked = False


# --------------------------------------------------------------------------- #
# Fachada usada por el detector
# --------------------------------------------------------------------------- #
class Alerts:
    def __init__(self, cfg, camera, base_dir, log):
        self.camera_name = camera["name"]
        self.log = log
        self.siren = None
        self.telegram = None
        self.recorder = None
        self._latest = None  # (resultado YOLO, n_gatos, momento) del último frame
        self._lock = threading.Lock()
        self._visit = None
        self._compilations = {}  # visit.id -> datos para generar/enviar la recopilación

        self.compilation_max_seconds = float(cfg["compilation_max_seconds"])
        self.photo_interval = float(cfg["telegram_interval_seconds"])
        self.reduced_photo_interval = float(cfg["reduced_photo_interval_seconds"])
        self.reduced_video_interval = float(cfg["reduced_video_interval_seconds"])
        self.segment_seconds = float(cfg["video_segment_seconds"])
        self.ask_after = float(cfg["long_visit_ask_after_seconds"])
        self.reask_after = float(cfg["long_visit_reask_minutes"]) * 60

        if cfg["siren_enabled"]:
            sound = cfg.get("siren_sound") or ""
            if sound:
                path = Path(sound) if Path(sound).is_absolute() else base_dir / sound
            else:
                path = base_dir / "sonidos" / "sirena.wav"
                if not path.exists():
                    generate_siren(path)
            siren = Siren(path, float(cfg["siren_cooldown_seconds"]))
            if not path.exists():
                log(f"Sirena desactivada: no existe {path}.")
            elif not siren.available:
                log("Sirena desactivada: no encontré reproductor de audio (afplay/paplay/aplay).")
            else:
                self.siren = siren
                log(f"Sirena lista ({path.name}, cada {siren.cooldown:g} s como máximo).")

        if cfg["video_enabled"]:
            if not shutil.which("ffmpeg"):
                log("Video desactivado: no encontré ffmpeg.")
            else:
                self.recorder = VideoRecorder(
                    base_dir / cfg["snapshots_dir"],
                    self.segment_seconds,
                    float(cfg["video_preroll_seconds"]),
                    int(cfg["video_max_side"]),
                    self._on_segment,
                    log,
                    draw_boxes=bool(cfg["video_with_boxes"]),
                    draw_clock=bool(cfg["video_with_clock"]),
                )
                self.recorder.start()
                log(f"Video listo (segmentos de {self.segment_seconds:g} s, {cfg['video_preroll_seconds']:g} s de pregrabación).")

        if cfg["telegram_enabled"]:
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            if not token:
                log("Telegram desactivado: falta TELEGRAM_BOT_TOKEN en .env.")
            else:
                notifier = TelegramNotifier(
                    token,
                    base_dir / "suscriptores.json",
                    self.camera_name,
                    log,
                    snapshot_provider=self._current_snapshot,
                    on_choice=self._on_choice,
                )
                try:
                    notifier.start()
                except Exception as e:
                    log(f"Telegram desactivado: {e}")
                else:
                    self.telegram = notifier
                    log(f"Telegram: bot @{notifier.bot_name} listo, {notifier.subscriber_count} suscriptor(es).")

    # --- Llamadas desde el detector ---------------------------------------
    def push_frame(self, frame):
        """Cada frame de la cámara (hilo lector), para el video."""
        if self.recorder:
            self.recorder.push(frame)

    def update_frame(self, result, n_cats):
        """Cada frame procesado por YOLO (hilo principal)."""
        self._latest = (result, n_cats, datetime.now())
        if self.recorder:
            boxes = []
            if n_cats:
                xyxy = result.boxes.xyxy.cpu().numpy().tolist()
                conf = result.boxes.conf.cpu().numpy().tolist()
                boxes = [(*b, c) for b, c in zip(xyxy, conf)]
            h, w = result.orig_shape
            self.recorder.set_detections(boxes, (w, h))
        self._check_visit_end()

    def on_cat(self, img, n_cats, confidence):
        """Cada frame con gato; cada alerta respeta su propio intervalo."""
        now = time.monotonic()
        ask = False
        with self._lock:
            visit = self._visit
            if visit is None:
                visit = self._visit = Visit(now, now + self.ask_after)
                self.log("Empieza una visita.")
            visit.last_seen = now
            if self.telegram and visit.mode in ("normal", "reduced") and now >= visit.next_question_at:
                visit.mode = "waiting"
                ask = True
            mode = visit.mode
        if ask:
            threading.Thread(target=self._ask_admin, args=(visit,), daemon=True).start()

        if self.siren and self.siren.play():
            self.log("Sirena sonando.")
        if self.recorder and mode != "stopped":
            self.recorder.cat_seen()
        if self.telegram and mode in ("normal", "reduced"):
            interval = self.photo_interval if mode == "normal" else self.reduced_photo_interval
            caption = (
                f"Gato detectado en {self.camera_name}\n"
                f"{datetime.now():%Y-%m-%d %H:%M:%S} · {n_cats} gato(s), confianza {confidence:.0%}"
            )
            self.telegram.notify(img, caption, interval)

    def stop(self):
        if self.recorder:
            self.recorder.stop()
        if self.siren:
            self.siren.stop()
        if self.telegram:
            self.telegram.stop()

    # --- Internos ----------------------------------------------------------
    def _current_snapshot(self):
        # Se llama desde el hilo del bot; plot() devuelve una copia nueva.
        latest = self._latest
        if latest is None:
            return None
        result, n_cats, taken_at = latest
        return result.plot(), n_cats, taken_at

    def _on_segment(self, path, info):
        """Un segmento de video quedó guardado (hilo del grabador)."""
        if info["final"]:
            return  # el programa se está cerrando: solo se guarda
        now = time.monotonic()
        with self._lock:
            visit = self._visit
            if visit is None:
                return
            visit.videos += 1
            visit.segments.append((path, info))
            n = visit.videos
            send = self.telegram is not None and (
                visit.mode == "normal"
                or (visit.mode == "reduced" and now - visit.last_video_sent >= self.reduced_video_interval)
            )
            if send:
                visit.last_video_sent = now
        if send:
            caption = (
                f"Video {n} de esta visita · {self.camera_name}\n"
                f"{info['started_at']:%Y-%m-%d %H:%M:%S} · {info['duration']:.0f} s"
            )
            self.telegram.send_video(path, caption, info)

    def _options(self):
        return [
            (f"Seguir igual (foto c/{self.photo_interval:g} s, video c/{self.segment_seconds:g} s)", "seguir"),
            (
                f"Menos seguido (foto c/{fmt_duration(self.reduced_photo_interval)}, "
                f"video c/{fmt_duration(self.reduced_video_interval)})",
                "menos",
            ),
            ("Dejar de grabar hasta que se vaya", "parar"),
        ]

    def _ask_admin(self, visit):
        admin = self.telegram.admin_chat_id
        if admin is None:
            with self._lock:
                if visit.mode == "waiting":
                    visit.mode = "normal"
                    visit.next_question_at = time.monotonic() + self.reask_after
            return
        text = (
            f"El gato lleva {fmt_duration(time.monotonic() - visit.started)} en {self.camera_name} "
            f"({visit.videos} video(s) hasta ahora).\n"
            "Mientras decides, sigo grabando pero no envío nada.\n\n¿Qué hago?"
        )
        message_id = self.telegram.ask(admin, text, self._options())
        ended = False
        with self._lock:
            if message_id is None:
                if visit.mode == "waiting":  # reintenta en un minuto
                    visit.mode = "normal"
                    visit.next_question_at = time.monotonic() + 60
                return
            visit.asked = True
            if self._visit is visit and visit.mode == "waiting":
                visit.question = (admin, message_id, text)
            else:
                ended = self._visit is not visit
        if ended:
            self.telegram.edit(admin, message_id, f"{text}\n\nEl gato ya se fue.")
        self.log("Telegram: le pregunté al administrador si sigo grabando.")

    def _on_choice(self, from_id, chat_id, message_id, data):
        """Botón pulsado en Telegram (hilo del bot)."""
        if data.startswith("rec:"):
            return self._on_compilation_choice(chat_id, data)
        if self.telegram is None or from_id != self.telegram.admin_chat_id:
            return "Solo el administrador puede responder.", None
        now = time.monotonic()
        reask = fmt_duration(self.reask_after)
        with self._lock:
            visit = self._visit
            if (
                visit is None
                or visit.mode != "waiting"
                or (visit.question is not None and visit.question[1] != message_id)
            ):
                return "Esta pregunta ya no está activa.", "(Pregunta vencida)"
            if data == "seguir":
                visit.mode = "normal"
                visit.next_question_at = now + self.reask_after
                text = f"Elegiste: seguir igual. Si sigue ahí, te vuelvo a preguntar en {reask}."
            elif data == "menos":
                visit.mode = "reduced"
                visit.next_question_at = now + self.reask_after
                text = f"Elegiste: menos seguido. Si sigue ahí, te vuelvo a preguntar en {reask}."
            elif data == "parar":
                visit.mode = "stopped"
                if self.recorder:
                    self.recorder.enabled = False
                text = "Elegiste: dejar de grabar. No envío nada hasta que el gato se vaya; te aviso cuando se vaya."
            else:
                return "Opción desconocida.", None
            visit.question = None
        self.log(f"Telegram: el administrador eligió '{data}'.")
        return "Listo", text

    def _check_visit_end(self):
        with self._lock:
            visit = self._visit
            if visit is None or time.monotonic() - visit.last_seen < VISIT_END_SECONDS:
                return
            self._visit = None
            if self.recorder:
                self.recorder.enabled = True
        duration = fmt_duration(visit.last_seen - visit.started)
        self.log(f"Termina la visita ({duration}, {visit.videos} video(s)).")
        if self.telegram and (visit.asked or len(visit.segments) >= 2):
            threading.Thread(target=self._notify_visit_end, args=(visit, duration), daemon=True).start()

    def _notify_visit_end(self, visit, duration):
        if visit.question:  # nadie respondió
            chat_id, message_id, text = visit.question
            self.telegram.edit(chat_id, message_id, f"{text}\n\nEl gato se fue tras {duration} sin que respondieras.")

        segments = [(p, i) for p, i in visit.segments if Path(p).exists()]
        end_text = f"El gato se fue tras {duration}"
        end_text += f" ({len(segments)} videos)." if segments else "."
        if visit.asked:
            end_text += " Todo vuelve a lo normal."

        if len(segments) < 2:
            admin = self.telegram.admin_chat_id
            if admin is not None:
                self.telegram.send_text(admin, end_text)
            return

        self._compilations[visit.id] = {
            "segments": segments,
            "started_at": visit.started_at,
            "ended_at": datetime.now(),
            "duration_text": duration,
            "lock": threading.Lock(),
            "result": None,
            "file_id": None,
        }
        for old in sorted(self._compilations)[:-20]:  # solo las últimas 20 visitas
            self._compilations.pop(old, None)

        options = [("Sí", f"rec:{visit.id}:si"), ("No", f"rec:{visit.id}:no")]
        question = f"{end_text}\n\n¿Quieres una recopilación de los videos de esta visita?"
        for chat_id in self.telegram.subscriber_ids:
            self.telegram.ask(chat_id, question, options, per_row=2)
        self.log(f"Telegram: ofrecí la recopilación de {len(segments)} videos.")

    def _on_compilation_choice(self, chat_id, data):
        try:
            _, visit_id, answer = data.split(":")
            comp = self._compilations.get(int(visit_id))
        except ValueError:
            return "Opción desconocida.", None
        if answer == "no":
            return "Listo", "Elegiste: no."
        if comp is None:
            return "Ya no está disponible.", "La recopilación ya no está disponible (el detector se reinició)."
        threading.Thread(target=self._send_compilation, args=(comp, chat_id), daemon=True).start()
        return "Preparando…", "Elegiste: sí. Preparando la recopilación, llega en unos segundos."

    def _send_compilation(self, comp, chat_id):
        # El lock hace que se genere una sola vez y que los demás reusen el file_id.
        with comp["lock"]:
            segments = comp["segments"]
            if comp["result"] is None:
                first = Path(segments[0][0])
                out = first.parent / f"recopilacion_{comp['started_at']:%H-%M-%S}.mp4"
                total = sum(info["duration"] for _, info in segments)
                self.log(f"Generando recopilación de {len(segments)} videos ({fmt_duration(total)})...")
                meta = build_compilation(
                    [p for p, _ in segments], total, out, max_seconds=self.compilation_max_seconds, log=self.log
                )
                if meta is None:
                    self.telegram.send_text(chat_id, "No se pudo generar la recopilación.")
                    return
                comp["result"] = (out, meta)
                self.log(f"Recopilación lista: {out.parent.name}/{out.name} ({meta['bytes'] / 1e6:.1f} MB).")

            out, meta = comp["result"]
            caption = (
                f"Recopilación de la visita · {self.camera_name}\n"
                f"{comp['started_at']:%Y-%m-%d %H:%M}–{comp['ended_at']:%H:%M} · "
                f"{len(segments)} videos · {comp['duration_text']}"
            )
            if meta["speed"] > 1.05:
                caption += f"\nAcelerada x{meta['speed']:.1f} para que dure {fmt_duration(meta['duration'])}."
            file_id = self.telegram.send_video_to(
                chat_id, out, caption, segments[0][1]["size"], meta["duration"], comp["file_id"]
            )
            if file_id:
                comp["file_id"] = file_id
                self.log(f"Telegram: recopilación enviada a {chat_id}.")
