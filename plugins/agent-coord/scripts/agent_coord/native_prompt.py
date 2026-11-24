"""Track first-prompt delivery independently of opening or resuming a native session."""


class NativePromptState:
    def __init__(self, store, provider, db):
        self.store, self.provider = store, provider
        self.pending = {}
        # An independent table also permits older adapters' positional INSERTs.
        db.execute("""CREATE TABLE IF NOT EXISTS native_prompt_initialization (
            provider TEXT NOT NULL, thread_id TEXT NOT NULL, native_id TEXT NOT NULL, started INTEGER NOT NULL,
            PRIMARY KEY (provider, thread_id, native_id))""")

    def bind(self, db, thread_id, native_id, *, resumed):
        # Existing builds bootstrapped every turn. A completed legacy turn
        # proves the prefix was sent; opening an unused conversation does not.
        db.execute("""INSERT OR IGNORE INTO native_prompt_initialization VALUES
            (?, ?, ?, ? AND EXISTS (SELECT 1 FROM turn_completions
                WHERE thread_id=? AND status='completed'))""",
            (self.provider, thread_id, native_id, resumed, thread_id))
        started = db.execute("""SELECT started FROM native_prompt_initialization
            WHERE provider=? AND thread_id=? AND native_id=?""", (self.provider, thread_id, native_id)).fetchone()[0]
        if started:
            self.pending.pop(thread_id, None)
        else:
            self.pending[thread_id] = native_id

    def confirm(self, thread_id):
        native_id = self.pending.get(thread_id)
        if native_id is None:
            return
        with self.store.database_operation("provider.prompt_initialized"), self.store._connection() as db:
            db.execute("""UPDATE native_prompt_initialization SET started=1
                WHERE provider=? AND thread_id=? AND native_id=?""", (self.provider, thread_id, native_id))
        self.pending.pop(thread_id, None)
