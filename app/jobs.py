"""
Tâches de fond (import, réindexation, recalcul des tuiles, sync Garmin) avec suivi
d'avancement et annulation.

Chaque file ("lane") exécute ses tâches une par une, dans l'ordre : les opérations
lourdes sur les données ne se marchent pas dessus, tandis que la sync Garmin (surtout
réseau) a sa propre file et ne bloque pas un rescan lancé depuis l'admin.

Le moteur reçoit la tâche comme objet "progress" : phase(nom, total), advance(),
check_cancel(). Hors tâche, NULL_PROGRESS fournit la même interface sans effet.
"""
import itertools
import logging
import queue
import threading
import time
from collections import deque

log = logging.getLogger("jobs")


class JobCancelled(BaseException):
    """Levée dans la tâche quand son annulation a été demandée. Dérive de BaseException
    pour ne pas être avalée par les `except Exception` qui protègent le traitement d'un
    fichier ou d'une activité isolée."""


class NullProgress:
    def phase(self, name, total=None):
        pass

    def advance(self, n=1, phase=None):
        pass

    def check_cancel(self):
        pass


NULL_PROGRESS = NullProgress()


class Job:
    def __init__(self, job_id, kind, label, lane, fn):
        self.id = job_id
        self.kind = kind
        self.label = label
        self.lane = lane
        self.fn = fn
        self.status = "queued"  # queued, running, done, failed, cancelled
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.result = None
        self.error = None
        self._phases = {}  # nom -> {"name", "done", "total", "started_at"}
        self._current = None
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._finished = threading.Event()

    # --- interface "progress" utilisée par le moteur ---

    def phase(self, name, total=None):
        """Démarre (ou redémarre) une phase ; total=None pour un avancement indéterminé."""
        self.check_cancel()
        with self._lock:
            self._phases.pop(name, None)  # une phase redémarrée repasse en dernière position
            self._phases[name] = {"name": name, "done": 0, "total": total, "started_at": time.time()}
            self._current = name

    def advance(self, n=1, phase=None):
        with self._lock:
            p = self._phases.get(phase or self._current)
            if p is not None:
                p["done"] += n
        self.check_cancel()

    def check_cancel(self):
        if self._cancel.is_set():
            raise JobCancelled()

    # --- état ---

    @property
    def cancel_requested(self):
        return self._cancel.is_set()

    def wait(self, timeout=None) -> bool:
        return self._finished.wait(timeout)

    def to_dict(self) -> dict:
        now = time.time()
        with self._lock:
            phases = [dict(p) for p in self._phases.values()]
            current = self._current
        for p in phases:
            p["current"] = p["name"] == current and self.status == "running"
            elapsed = now - p.pop("started_at")
            p["eta_s"] = None
            if p["current"] and p["total"] and 0 < p["done"] < p["total"]:
                p["eta_s"] = round(elapsed / p["done"] * (p["total"] - p["done"]))
        end = self.finished_at or now
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "lane": self.lane,
            "status": self.status,
            "cancel_requested": self.cancel_requested,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(end - self.started_at, 1) if self.started_at else None,
            "phases": phases,
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    def __init__(self, lanes=("data", "garmin"), history=100):
        self._lock = threading.Lock()
        self._jobs = {}  # id -> Job, dans l'ordre de création
        self._finished = deque()
        self._history = history
        self._ids = itertools.count(1)
        self._queues = {lane: queue.Queue() for lane in lanes}
        for lane in lanes:
            threading.Thread(target=self._worker, args=(lane,), daemon=True, name=f"jobs-{lane}").start()

    def submit(self, kind, label, fn, lane="data", unique=False):
        """Ajoute une tâche ; fn(job) retourne un dict de résultat. Avec unique=True, si
        une tâche du même type est déjà en attente ou en cours, elle est renvoyée au lieu
        d'en créer une seconde. Retourne (tâche, créée)."""
        with self._lock:
            if unique:
                for job in self._jobs.values():
                    if job.kind == kind and job.status in ("queued", "running"):
                        return job, False
            job = Job(next(self._ids), kind, label, lane, fn)
            self._jobs[job.id] = job
        self._queues[lane].put(job)
        return job, True

    def cancel(self, job_id) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in ("queued", "running"):
                return False
            job._cancel.set()
            if job.status == "queued":
                self._finish(job, "cancelled")
        return True

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def active(self, kind=None):
        with self._lock:
            return [j for j in self._jobs.values()
                    if j.status in ("queued", "running") and (kind is None or j.kind == kind)]

    def list(self, limit=50):
        """Tâches les plus récentes d'abord."""
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.to_dict() for j in reversed(jobs[-limit:])]

    def _finish(self, job, status):
        # appelé avec self._lock
        job.status = status
        job.finished_at = time.time()
        job._finished.set()
        self._finished.append(job.id)
        while len(self._finished) > self._history:
            self._jobs.pop(self._finished.popleft(), None)

    def _worker(self, lane):
        q = self._queues[lane]
        while True:
            job = q.get()
            with self._lock:
                if job.status != "queued":  # annulée pendant l'attente
                    continue
                job.status = "running"
                job.started_at = time.time()
            log.info("Tâche #%d démarrée : %s", job.id, job.label)
            status = "done"
            try:
                job.result = job.fn(job)
            except JobCancelled:
                status = "cancelled"
                log.warning("Tâche #%d annulée : %s", job.id, job.label)
            except Exception as e:
                status = "failed"
                job.error = str(e) or e.__class__.__name__
                log.exception("Tâche #%d en échec : %s", job.id, job.label)
            with self._lock:
                self._finish(job, status)
            if status == "done":
                log.info("Tâche #%d terminée en %.1f s : %s", job.id, job.finished_at - job.started_at, job.label)


class LogBuffer(logging.Handler):
    """Garde en mémoire les derniers messages de log, pour le journal de l'admin."""

    def __init__(self, capacity=2000):
        super().__init__(level=logging.INFO)
        self._entries = deque(maxlen=capacity)
        self._seq = itertools.count(1)
        self._buffer_lock = threading.Lock()

    def emit(self, record):
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            entry = {"seq": None, "ts": record.created, "level": record.levelname,
                     "logger": record.name, "message": message}
            with self._buffer_lock:
                entry["seq"] = next(self._seq)
                self._entries.append(entry)
        except Exception:
            self.handleError(record)

    def since(self, after=0, min_level="INFO", limit=500):
        threshold = logging.getLevelName(min_level) if isinstance(min_level, str) else min_level
        if not isinstance(threshold, int):
            threshold = logging.INFO
        with self._buffer_lock:
            entries = [e for e in self._entries if e["seq"] > after]
        entries = [e for e in entries if logging.getLevelName(e["level"]) >= threshold]
        return entries[-limit:]
