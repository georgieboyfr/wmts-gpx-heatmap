import logging
import threading

from app.jobs import JobManager, LogBuffer


def test_job_progress_result_and_eta():
    jobs = JobManager(lanes=("data",))
    gate = threading.Event()

    def run(job):
        job.phase("Lecture", 4)
        job.advance(2)
        gate.wait(5)
        job.advance(2)
        return {"ok": 1}

    job, created = jobs.submit("x", "Test", run)
    assert created
    # pendant l'exécution : avancement partiel et estimation du temps restant
    for _ in range(100):
        d = job.to_dict()
        if d["phases"] and d["phases"][0]["done"] == 2:
            break
        threading.Event().wait(0.02)
    phase = d["phases"][0]
    assert d["status"] == "running" and phase["current"] and phase["total"] == 4 and phase["eta_s"] is not None
    gate.set()
    assert job.wait(5)
    assert job.status == "done" and job.result == {"ok": 1}


def test_unique_jobs_are_not_duplicated_and_queue_runs_in_order():
    jobs = JobManager(lanes=("data",))
    gate = threading.Event()
    order = []
    first, _ = jobs.submit("a", "A", lambda job: (gate.wait(5), order.append("a")))
    second, created = jobs.submit("b", "B", lambda job: order.append("b"), unique=True)
    again, created_again = jobs.submit("b", "B", lambda job: order.append("b2"), unique=True)
    assert created and not created_again and again is second
    gate.set()
    assert second.wait(5)
    assert order == ["a", "b"]


def test_cancel_queued_and_running_jobs():
    jobs = JobManager(lanes=("data",))
    started = threading.Event()

    def long_job(job):
        job.phase("Boucle", None)
        started.set()
        while True:
            job.advance()

    running, _ = jobs.submit("long", "Long", long_job)
    queued, _ = jobs.submit("next", "Suivante", lambda job: {"ran": True})
    assert started.wait(5)
    assert jobs.cancel(queued.id) and jobs.cancel(running.id)
    assert running.wait(5) and queued.wait(5)
    assert running.status == "cancelled" and queued.status == "cancelled" and queued.result is None
    assert not jobs.cancel(running.id)  # déjà terminée


def test_failed_job_keeps_error_message():
    jobs = JobManager(lanes=("data",))
    job, _ = jobs.submit("x", "X", lambda job: 1 / 0)
    assert job.wait(5)
    assert job.status == "failed" and "division" in job.error


def test_log_buffer_filters_by_sequence_and_level():
    buf = LogBuffer(capacity=10)
    logger = logging.getLogger("test-buffer")
    logger.addHandler(buf)
    logger.setLevel(logging.INFO)
    try:
        logger.info("un")
        logger.warning("deux")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("trois")
    finally:
        logger.removeHandler(buf)
    entries = buf.since()
    assert [e["message"].split("\n")[0] for e in entries] == ["un", "deux", "trois"]
    assert "ValueError: boom" in entries[2]["message"]
    assert [e["message"] for e in buf.since(entries[0]["seq"], "WARNING")][0] == "deux"
