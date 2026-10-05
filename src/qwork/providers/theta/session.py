"""Share one vendor session across API, diagnostics and research processes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace


class ThetaSessionCache:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "private" / "theta-session.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, api_key, factory, previous_session=None):
        from thetadata._proto.endpoints_pb2 import AuthToken

        key_id = hashlib.sha256(api_key.encode()).hexdigest()
        with closing(sqlite3.connect(self.path, timeout=30)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS sessions (key_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            # Serialize authentication only. Data requests remain independent.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM sessions WHERE key_id=?", (key_id,)).fetchone()
            saved = json.loads(row[0]) if row else None
            if saved and (previous_session is None or saved["session_uuid"] != previous_session):
                authorized = SimpleNamespace(
                    auth_token=AuthToken(session_uuid=saved["session_uuid"]),
                    **{key: value for key, value in saved.items() if key != "session_uuid"},
                )
                return factory(existing_authorized_client=authorized)
            client = factory(api_key=api_key)
            payload = {
                "session_uuid": client.auth_token.session_uuid,
                **{
                    field: getattr(client, field)
                    for field in (
                        "email",
                        "stock_subscription",
                        "options_subscription",
                        "index_subscription",
                        "mdds_type",
                        "mdds_host",
                        "mdds_port",
                    )
                },
            }
            connection.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (key_id, json.dumps(payload)))
            return client
