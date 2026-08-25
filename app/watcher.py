import logging
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger("watcher")


class GpxHandler(FileSystemEventHandler):
    def __init__(self, engine):
        self.engine = engine

    def on_created(self, event):
        self._maybe_ingest(event.src_path)

    def on_moved(self, event):
        self._maybe_ingest(event.dest_path)

    def _maybe_ingest(self, path):
        if not path.lower().endswith(".gpx"):
            return
        # laisse le temps au fichier de finir de s'écrire (copie réseau, etc.)
        time.sleep(1.5)
        try:
            log.info("Ingestion de %s", path)
            self.engine.ingest_gpx(path)
            log.info("OK: %s", path)
        except Exception:
            log.exception("Echec ingestion %s", path)


def start_watcher(engine, watch_dir: str):
    Path(watch_dir).mkdir(parents=True, exist_ok=True)

    # ingère ce qui est déjà présent au démarrage en UN SEUL lot (bulk_ingest) :
    # chaque tuile n'est alors recalculée qu'une fois pour tout le lot, au lieu
    # d'être redessinée à chaque fichier (bien plus rapide sur un import initial
    # de centaines de GPX)
    existing = [str(p) for p in Path(watch_dir).glob("*.gpx") if not engine.already_ingested(str(p))]
    if existing:
        log.info("Import initial groupé de %d fichier(s)...", len(existing))
        try:
            engine.bulk_ingest(existing)
            log.info("Import initial terminé.")
        except Exception:
            log.exception("Echec de l'import initial groupé")

    handler = GpxHandler(engine)
    observer = Observer()
    observer.schedule(handler, watch_dir, recursive=False)
    observer.daemon = True
    observer.start()
    return observer
