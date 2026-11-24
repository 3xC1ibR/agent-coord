"""Serialize saved opening responses without decoding immutable item bodies."""
from .transcript_store import TranscriptStore, encode


def read_snapshot_json(sessions, thread_id, *, recent=False, controls=False):
    """Preserve normal snapshot projection, with a bounded saved-history reader.

    Resident histories, lifecycle recovery, reviews and retained legacy data use
    the existing parsed path. Only the stable indexed window is serialized here.
    The identity check also discards a prepared body if read-side concurrency
    caused snapshot projection to choose a different history.
    """
    prepared = None

    def saved_history_reader(identity):
        nonlocal prepared
        prepared = None
        if sessions.reviews.list(identity):
            return None
        value = TranscriptStore(sessions.store).read_window_projection(identity)
        if value is None:
            return None
        projection, body = value
        prepared = projection, dict(projection), body
        return projection

    with sessions.store.reading():
        if not recent:
            result = sessions.read(thread_id, recent=False, snapshot=True)
        elif sessions.store.threads.is_browser_session(thread_id):
            result = sessions._read_snapshot(thread_id, recent=True, saved_history_reader=saved_history_reader)
        else:
            # External snapshots have their own normal public fields and scope
            # checks. Do not substitute the browser-session projection.
            result = sessions._read_external_snapshot(sessions.work_thread(thread_id), recent=True,
                                                       saved_history_reader=saved_history_reader)
        if prepared is not None:
            projection, original, body = prepared
            if result["thread"] is not projection or result.get("reviews"):
                # A review appeared after the initial check or a concurrent
                # projection invalidated this history. Rebuild with full items.
                result = sessions.read(thread_id, recent=recent, snapshot=True)
                prepared = None
        if controls:
            from .thread_control import ThreadControl
            work = result["work_thread"]
            work["close_when_done"] = ThreadControl(sessions.store).status(work["thread_id"])
    if prepared is None:
        return encode(result)
    extra = {key: value for key, value in projection.items() if key not in original}
    if extra:
        body = body[:-1] + b"," + encode(extra)[1:]
    header = encode({key: value for key, value in result.items() if key != "thread"})[:-1]
    return header + b',"thread":' + body + b"}"
