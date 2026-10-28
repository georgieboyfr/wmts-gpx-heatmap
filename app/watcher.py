"""
Surveillance du dossier GPX.

Les événements du système de fichiers ne sont pas traités immédiatement : chaque
chemin est mis en attente jusqu'à ce que le fichier n'ait plus bougé pendant
`settle_s` secondes (copie réseau lente, gros fichier...). Tous les fichiers prêts
au même moment sont ensuite transmis en UN SEUL lot à `on_ready(à_importer, à_retirer)`
(l'application en fait une tâche d'import, visible dans l'admin) ; un fichier supprimé
ou renommé y figure parmi les fichiers à retirer.
"""
import logging
import os
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .gpx_utils import is_gpx, normalize_path

log = logging.getLogger("watcher")


def _signature(path):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return st.st_mtime, st.st_size


class GpxHandler(FileSystemEventHandler):
    def __init__(self, on_ready, settle_s=2.0, ignore=None):
        """on_ready(à_importer, à_retirer) : appelé avec les listes de chemins prêts.
        ignore(path) -> bool : fichiers à laisser de côté (ex : en cours de traitement
        par la sync Garmin, qui les ingère elle-même par lots)."""
        self.on_ready = on_ready
        self.settle_s = settle_s
        self.ignore = ignore or (lambda path: False)
        self._pending = {}  # chemin -> (échéance, signature au moment du dernier événement)
        self._cond = threading.Condition()
        threading.Thread(target=self._worker, daemon=True, name="gpx-watcher").start()

    def on_created(self, event):
        self._on_event(event)

    def on_modified(self, event):
        self._on_event(event)

    def on_closed(self, event):
        self._on_event(event)

    def on_deleted(self, event):
        self._on_event(event)

    def on_moved(self, event):
        if not event.is_directory:
            self.schedule(event.src_path)
            self.schedule(event.dest_path)

    def _on_event(self, event):
        if not event.is_directory:
            self.schedule(event.src_path)

    def schedule(self, path):
        if not is_gpx(path):
            return
        path = normalize_path(path)
        with self._cond:
            self._pending[path] = (time.monotonic() + self.settle_s, _signature(path))
            self._cond.notify()

    def _worker(self):
        while True:
            with self._cond:
                while not self._pending:
                    self._cond.wait()
                now = time.monotonic()
                next_deadline = min(deadline for deadline, _ in self._pending.values())
                if next_deadline > now:
                    self._cond.wait(next_deadline - now)
                    continue
                ready = {p: sig for p, (deadline, sig) in self._pending.items() if deadline <= now}
                for p in ready:
                    del self._pending[p]
            self._process(ready)

    def _process(self, ready):
        to_ingest, to_remove = [], []
        for path, sig in ready.items():
            current = _signature(path)
            if current is None:
                to_remove.append(path)
            elif current != sig:
                self.schedule(path)  # encore en cours d'écriture : on attend encore
            elif not self.ignore(path):
                to_ingest.append(path)
        if not to_ingest and not to_remove:
            return
        try:
            self.on_ready(to_ingest, to_remove)
        except Exception:
            log.exception("Echec du traitement de %s", ", ".join(to_remove + to_ingest))


def start_watcher(on_ready, watch_dir: str, ignore=None, settle_s=2.0):
    """Démarre la surveillance (sans import initial : voir HeatmapEngine.sync_folder)."""
    Path(watch_dir).mkdir(parents=True, exist_ok=True)
    handler = GpxHandler(on_ready, settle_s=settle_s, ignore=ignore)
    observer = Observer()
    observer.schedule(handler, watch_dir, recursive=False)
    observer.daemon = True
    observer.start()
    return observer
