from app.watcher import GpxHandler, start_watcher

from .conftest import wait_until, write_gpx


def engine_callback(engine):
    def on_ready(to_ingest, to_remove):
        engine.remove_files(to_remove)
        engine.bulk_ingest(to_ingest)
    return on_ready


def test_handler_ingests_then_removes(engine, gpx_dir):
    handler = GpxHandler(engine_callback(engine), settle_s=0.1)
    p = write_gpx(gpx_dir / "a.gpx", [(48.85, 2.0), (48.85, 2.01)])
    handler.schedule(str(p))
    assert wait_until(lambda: engine.point_count == 2)
    p.unlink()
    handler.schedule(str(p))
    assert wait_until(lambda: engine.point_count == 0)


def test_handler_batches_ready_files(gpx_dir):
    calls = []
    handler = GpxHandler(lambda ingest, remove: calls.append((sorted(ingest), remove)), settle_s=0.2)
    paths = [write_gpx(gpx_dir / f"{i}.gpx", [(48.85, 2.0)]) for i in range(3)]
    for p in paths:
        handler.schedule(str(p))
    # prêts quasiment en même temps : transmis en un lot (ou deux si l'échéance tombe entre eux)
    assert wait_until(lambda: sum(len(ingest) for ingest, _ in calls) == 3)
    assert len(calls) <= 2


def test_handler_skips_ignored_and_non_gpx(gpx_dir):
    calls = []
    handler = GpxHandler(lambda ingest, remove: calls.append(ingest), settle_s=0.05, ignore=lambda path: True)
    p = write_gpx(gpx_dir / "a.gpx", [(48.85, 2.0), (48.85, 2.01)])
    handler.schedule(str(p))
    handler.schedule(str(gpx_dir / "a.gpx.part"))
    assert not wait_until(lambda: calls, timeout=0.5)


def test_observer_picks_up_new_files(engine, gpx_dir):
    observer = start_watcher(engine_callback(engine), str(gpx_dir), settle_s=0.2)
    try:
        write_gpx(gpx_dir / "a.gpx", [(48.85, 2.0), (48.85, 2.01)])
        assert wait_until(lambda: engine.point_count == 2)
    finally:
        observer.stop()
        observer.join()
