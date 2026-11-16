"""Alertas del detector: sirena y fotos por Telegram."""

import hmac
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
    def __init__(self, code, description):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description or ""

    @property
    def chat_gone(self):
        """El usuario bloqueó el bot o el chat ya no existe."""
        return self.code == 403 or (self.code == 400 and "chat not found" in self.description.lower())


HELP_TEXT = (
    "Hola, soy el detector de gatos.\n\n"
    "/suscribir <clave> - recibir la foto cuando vea un gato\n"
    "/salir - dejar de recibir alertas\n"
    "/estado - ver si el detector está activo"
)


class TelegramNotifier:
    """Envía fotos a los suscriptores y atiende comandos del bot.

    - Un hilo hace long polling de getUpdates para /suscribir, /salir, etc.
    - Otro hilo envía las fotos, así la detección nunca espera a la red.
    Los suscriptores se guardan en un JSON local (no se sube al repo).
    """

    def __init__(self, token, subscribe_key, subscribers_path, interval, camera_name, log):
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}/"
        self.key = subscribe_key
        self.path = Path(subscribers_path)
        self.interval = interval
        self.camera_name = camera_name
        self.log = log
        self.bot_name = None
        self._lock = threading.Lock()
        self._subs = self._load()
        self._queue = queue.Queue(maxsize=3)
        self._last_sent = float("-inf")
        self._running = False
        self._sender = None
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
            raise TelegramError(data.get("error_code"), data.get("description"))
        return data["result"]

    def _reply(self, chat_id, text):
        try:
            self._call("sendMessage", data={"chat_id": chat_id, "text": text})
        except (TelegramError, requests.RequestException) as e:
            self.log(f"Telegram: no se pudo responder a {chat_id}: {self._safe(e)}")

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

    # --- Ciclo de vida -----------------------------------------------------
    def start(self):
        """Valida el token y arranca los hilos. Lanza excepción si falla."""
        try:
            self.bot_name = self._call("getMe", timeout=10)["username"]
        except requests.RequestException as e:
            raise RuntimeError(self._safe(e)) from None
        self._running = True
        self._sender = threading.Thread(target=self._send_loop, daemon=True)
        self._sender.start()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def stop(self):
        self._running = False
        if self._sender:
            self._sender.join(timeout=10)  # deja salir la última foto en cola

    # --- Envío de fotos ----------------------------------------------------
    def notify(self, img, caption):
        """Encola la foto si ya pasó el intervalo. Devuelve True si la encoló."""
        now = time.monotonic()
        if now - self._last_sent < self.interval or not self.subscriber_count:
            return False
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return False
        try:
            self._queue.put_nowait((buf.tobytes(), caption))
        except queue.Full:
            return False
        self._last_sent = now
        return True

    def _send_loop(self):
        while self._running or not self._queue.empty():
            try:
                photo, caption = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._lock:
                chat_ids = list(self._subs)
            sent = 0
            for chat_id in chat_ids:
                try:
                    self._call(
                        "sendPhoto",
                        data={"chat_id": chat_id, "caption": caption},
                        files={"photo": ("gato.jpg", photo, "image/jpeg")},
                    )
                    sent += 1
                except TelegramError as e:
                    if e.chat_gone:
                        self._remove(chat_id)
                        self.log(f"Telegram: {chat_id} eliminado de suscriptores ({e}).")
                    else:
                        self.log(f"Telegram: error enviando a {chat_id}: {e}")
                except requests.RequestException as e:
                    self.log(f"Telegram: error de red enviando a {chat_id}: {self._safe(e)}")
            self.log(f"Telegram: foto enviada a {sent}/{len(chat_ids)} suscriptor(es).")

    # --- Comandos ----------------------------------------------------------
    def _poll_loop(self):
        offset = None
        network_ok = True
        while self._running:
            data = {"timeout": 25, "allowed_updates": json.dumps(["message"])}
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
                msg = update.get("message")
                if msg and msg.get("text"):
                    try:
                        self._handle(msg)
                    except Exception as e:  # un mensaje raro no debe tumbar el hilo
                        self.log(f"Telegram: error procesando mensaje: {self._safe(e)}")

    def _handle(self, msg):
        chat = msg["chat"]
        chat_id = chat["id"]
        name = (
            chat.get("title")
            or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            or chat.get("username")
            or str(chat_id)
        )
        command, _, arg = msg["text"].strip().partition(" ")
        command = command.split("@")[0].lower()  # /suscribir@MiBot en grupos

        if command in ("/start", "/ayuda", "/help"):
            self._reply(chat_id, HELP_TEXT)

        elif command == "/suscribir":
            if not self.key:
                self._reply(chat_id, "La suscripción está desactivada (falta TELEGRAM_CLAVE en el detector).")
            elif not hmac.compare_digest(arg.strip().encode(), self.key.encode()):
                self._reply(chat_id, "Clave incorrecta.")
                self.log(f"Telegram: intento de suscripción con clave incorrecta de {name} ({chat_id}).")
            elif self._add(chat_id, name):
                self._reply(chat_id, "Listo, te enviaré la foto cuando vea un gato.")
                self.log(f"Telegram: nuevo suscriptor {name} ({chat_id}).")
            else:
                self._reply(chat_id, "Ya estabas suscrito.")

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


# --------------------------------------------------------------------------- #
# Fachada usada por el detector
# --------------------------------------------------------------------------- #
class Alerts:
    def __init__(self, cfg, camera, base_dir, log):
        self.camera_name = camera["name"]
        self.log = log
        self.siren = None
        self.telegram = None

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

        if cfg["telegram_enabled"]:
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            key = os.environ.get("TELEGRAM_CLAVE", "").strip()
            if not token:
                log("Telegram desactivado: falta TELEGRAM_BOT_TOKEN en .env.")
            else:
                notifier = TelegramNotifier(
                    token,
                    key,
                    base_dir / "suscriptores.json",
                    float(cfg["telegram_interval_seconds"]),
                    self.camera_name,
                    log,
                )
                try:
                    notifier.start()
                except Exception as e:
                    log(f"Telegram desactivado: {e}")
                else:
                    self.telegram = notifier
                    log(f"Telegram: bot @{notifier.bot_name} listo, {notifier.subscriber_count} suscriptor(es).")
                    if not key:
                        log("Telegram: falta TELEGRAM_CLAVE, nadie nuevo podrá usar /suscribir.")

    def on_cat(self, img, n_cats, confidence):
        """Llamar en cada frame con gato; cada alerta respeta su propio intervalo."""
        if self.siren and self.siren.play():
            self.log("Sirena sonando.")
        if self.telegram:
            caption = (
                f"Gato detectado en {self.camera_name}\n"
                f"{datetime.now():%Y-%m-%d %H:%M:%S} · {n_cats} gato(s), confianza {confidence:.0%}"
            )
            self.telegram.notify(img, caption)

    def stop(self):
        if self.siren:
            self.siren.stop()
        if self.telegram:
            self.telegram.stop()
