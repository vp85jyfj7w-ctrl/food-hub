"""Shopping Session (Food Hub, FoodHub-0002, brief 3.5).

A shopping session is "which retailer" metadata layered on top of whichever
scanner mode is active (almost always "inventory" / Stock up) -- it composes
with the four existing modes in services/scanner_mode.py rather than adding a
fifth one. At most one session is active (finished_at IS NULL) at a time;
starting a new one finishes whichever was open first, so there is never an
ambiguous "which trip is this scan part of" state.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models.db_models import Retailer, ShoppingSession


def current(db: Session) -> ShoppingSession | None:
    """The active session, or None."""
    return (db.query(ShoppingSession)
            .filter(ShoppingSession.finished_at.is_(None))
            .order_by(ShoppingSession.id.desc())
            .first())


def current_retailer_id(db: Session) -> int | None:
    """The active session's retailer id, or None. The one thing the scan-commit
    path needs; kept separate so callers don't have to know the session shape."""
    session = current(db)
    return session.retailer_id if session else None


def start(db: Session, retailer_id: int) -> ShoppingSession:
    """Begin a session at ``retailer_id``, finishing any session already open.

    Raises ValueError for an unknown/inactive retailer rather than silently
    starting a session with a dangling retailer_id.
    """
    retailer = db.query(Retailer).filter(Retailer.id == retailer_id,
                                         Retailer.active == 1).first()
    if not retailer:
        raise ValueError("Unknown or hidden retailer")
    existing = current(db)
    if existing:
        finish(db, existing.id)
    session = ShoppingSession(
        retailer_id=retailer_id,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        item_count=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def finish(db: Session, session_id: int | None = None) -> ShoppingSession | None:
    """Close the given session (or the active one when session_id is None).

    Returns None when there was nothing to finish, so a "Finish trip" button
    pressed twice (a double-tap, or two browser tabs) is a no-op rather than
    an error.
    """
    session = (db.query(ShoppingSession).filter(ShoppingSession.id == session_id).first()
              if session_id is not None else current(db))
    if not session or session.finished_at:
        return None
    session.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.commit()
    db.refresh(session)
    return session


def bump_item_count(db: Session, session_id: int) -> None:
    """Record one more item tagged under this session. Best-effort: a bookkeeping
    miss here must never block the stock write that triggered it."""
    try:
        session = db.query(ShoppingSession).filter(ShoppingSession.id == session_id).first()
        if session:
            session.item_count = (session.item_count or 0) + 1
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def as_dict(session: ShoppingSession | None, db: Session) -> dict | None:
    """JSON-ready view of a session (or None), with the retailer name resolved
    so callers never need a second query just to show it."""
    if not session:
        return None
    retailer = db.query(Retailer).filter(Retailer.id == session.retailer_id).first()
    return {
        "id": session.id,
        "retailer_id": session.retailer_id,
        "retailer_name": retailer.name if retailer else None,
        "started_at": session.started_at,
        "finished_at": session.finished_at,
        "item_count": session.item_count,
    }
