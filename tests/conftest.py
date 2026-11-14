from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from radar.db import init_db, make_engine, session_scope


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as db_session:
        yield db_session
