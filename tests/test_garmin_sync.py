from app.garmin_sync import GarminSync, _points_to_gpx_bytes, _tcx_to_points
from app.gpx_utils import parse_gpx_segments
from app.jobs import JobCancelled, JobManager

TCX = b"""<?xml version="1.0"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Activities><Activity><Lap><Track>
    <Trackpoint><Position><LatitudeDegrees>48.1</LatitudeDegrees><LongitudeDegrees>2.1</LongitudeDegrees></Position></Trackpoint>
    <Trackpoint><HeartRateBpm><Value>120</Value></HeartRateBpm></Trackpoint>
    <Trackpoint><Position><LatitudeDegrees>48.2</LatitudeDegrees><LongitudeDegrees>2.2</LongitudeDegrees></Position></Trackpoint>
  </Track></Lap></Activity></Activities>
</TrainingCenterDatabase>"""

GPX = b'<?xml version="1.0"?><gpx version="1.1"><trk><trkseg><trkpt lat="1" lon="2"></trkpt></trkseg></trk></gpx>'


def test_tcx_to_points_ignores_trackpoints_without_position():
    assert _tcx_to_points(TCX) == [(48.1, 2.1), (48.2, 2.2)]


def test_points_to_gpx_roundtrip(tmp_path):
    p = tmp_path / "a.gpx"
    p.write_bytes(_points_to_gpx_bytes([(48.1, 2.1), (48.2, 2.2)], name="a & <b>"))
    assert parse_gpx_segments(str(p))[0] == [[(48.1, 2.1), (48.2, 2.2)]]


class FakeClient:
    def __init__(self, n):
        self.acts = [{"activityId": i, "startTimeGMT": "2099-01-01 00:00:00"} for i in range(n)]

    def get_activities(self, start, limit):
        return self.acts[start:start + limit]

    def download_activity(self, act_id, dl_fmt):
        return GPX


def test_sync_batches_and_releases_claims(tmp_path):
    s = GarminSync(tmp_path / "gpx")
    seen = []

    def on_batch(paths):
        assert all(s.is_claimed(p) for p in paths)  # réservés tant que le lot n'est pas ingéré
        seen.append(len(paths))

    paths = s.sync(FakeClient(5), on_batch=on_batch, ingest_batch_size=2)
    assert len(paths) == 5 and seen == [2, 2, 1]
    assert not any(s.is_claimed(p) for p in paths)
    assert not list((tmp_path / "gpx").glob("*.part"))


def test_sync_flushes_downloaded_files_on_error(tmp_path):
    # 2 pages d'activités : la 1re passe, la 2e échoue -> les 50 fichiers déjà
    # téléchargés doivent quand même être transmis au lot
    client = FakeClient(60)
    original = client.get_activities

    def flaky(start, limit):
        if start > 0:
            raise ConnectionError("boom")
        return original(start, limit)

    client.get_activities = flaky
    s = GarminSync(tmp_path / "gpx")
    batches = []
    try:
        s.sync(client, on_batch=lambda p: batches.append(len(p)), ingest_batch_size=0)
    except ConnectionError:
        pass
    assert batches == [50]
    assert len(s.downloaded) == 50


def test_sync_reports_progress_and_can_be_cancelled(tmp_path):
    jobs = JobManager(lanes=("garmin",))
    s = GarminSync(tmp_path / "gpx")
    seen = {}

    def run(job):
        def on_batch(paths):
            seen["batch"] = len(paths)
            job.cancel_requested or jobs.cancel(job.id)  # annule après le 1er lot

        return s.sync(FakeClient(10), on_batch=on_batch, ingest_batch_size=3, progress=job)

    job, _ = jobs.submit("garmin", "test", run, lane="garmin")
    assert job.wait(10)
    assert job.status == "cancelled"
    assert seen["batch"] == 3
    assert len(s.downloaded) < 10
    phase = job.to_dict()["phases"][0]
    assert phase["name"] == "Téléchargement des activités" and phase["done"] == len(s.downloaded)


def test_cancelled_is_not_swallowed_by_per_activity_errors():
    assert not issubclass(JobCancelled, Exception)
