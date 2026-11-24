"""Serialize saved opening responses without decoding immutable item bodies."""
from contextlib import contextmanager

from .codex_app_server import BrowserBusyError
from .transcript_store import TranscriptStore, encode


class OpeningAdmissionError(BrowserBusyError):
    """Runtime admission rejected the opening before any work began."""


@contextmanager
def opening_admission(sessions):
    """Keep every opening projection inside runtime admission."""
    with sessions.admission.enter() as allowed:
        if not allowed:
            raise OpeningAdmissionError(sessions.admission.message(
                "Runtime maintenance is pausing new work. Try again shortly."))
        yield


def read_snapshot_json(sessions, thread_id, *, recent=False, controls=False):
    """Read a serialized opening with the same scope as indexed pagination."""
    with opening_admission(sessions):
        sessions._authorize_history(thread_id)
        return _read_snapshot_json(sessions, thread_id, recent=recent, controls=controls)


def _read_snapshot_json(sessions, thread_id, *, recent, controls):
    """Preserve normal snapshot projection, with a bounded saved-history reader.

    Resident histories, lifecycle recovery, reviews and retained legacy data use
    the existing parsed path. Only the stable indexed window is serialized here.
    The identity check also discards a prepared body if read-side concurrency
    caused snapshot projection to choose a different history.
    """
    prepared = None
    prepared_timeline = None

    def coordination_reader(store, identity, cwd):
        nonlocal prepared_timeline
        from .snapshot_cache import read_timeline_json
        marker = []
        prepared_timeline = marker, read_timeline_json(store, identity, cwd)
        return marker

    def saved_history_reader(identity):
        nonlocal prepared
        prepared = None
        if sessions.reviews.list(identity):
            return None
        value = TranscriptStore(sessions.store).read_window_projection(identity)
        if value is None:
            return None
        projection, turns = value
        prepared = projection, turns
        return projection

    with sessions.store.reading():
        if not recent:
            result = sessions.read(thread_id, recent=False, snapshot=True)
        elif sessions.store.threads.is_browser_session(thread_id):
            result = sessions._read_snapshot(thread_id, recent=True, saved_history_reader=saved_history_reader,
                                              coordination_reader=coordination_reader)
        else:
            # External snapshots have their own normal public fields and scope
            # checks. Do not substitute the browser-session projection.
            result = sessions._read_external_snapshot(sessions.work_thread(thread_id), recent=True,
                                                       saved_history_reader=saved_history_reader,
                                                       coordination_reader=coordination_reader)
        if prepared is not None:
            projection, turns = prepared
            if result["thread"] is not projection or result.get("reviews"):
                # A review appeared after the initial check or a concurrent
                # projection invalidated this history. Rebuild with full items.
                result = sessions.read(thread_id, recent=recent, snapshot=True)
                prepared = None
        if controls:
            from .thread_control import ThreadControl
            work = result["work_thread"]
            work["close_when_done"] = ThreadControl(sessions.store).status(work["thread_id"])
    raw = {}
    if prepared_timeline is not None and result["coordinationMessages"] is prepared_timeline[0]:
        raw["coordinationMessages"] = prepared_timeline[1]
    if prepared is not None:
        metadata = encode({key: value for key, value in projection.items() if key != "turns"})[:-1]
        raw["thread"] = metadata + (b"," if metadata != b"{" else b"") + b'"turns":' + turns + b"}"
    if not raw:
        return encode(result)
    header = encode({key: value for key, value in result.items() if key not in raw})[:-1]
    return header + b"," + b",".join(encode(key) + b":" + value for key, value in raw.items()) + b"}"
