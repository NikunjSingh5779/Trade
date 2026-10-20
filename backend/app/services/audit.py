from typing import Any

from app.storage import Database


class AuditService:
    def __init__(self, db: Database):
        self.db = db

    def record(self, event: str, details: dict[str, Any]) -> None:
        self.db.add_audit(event, details)
