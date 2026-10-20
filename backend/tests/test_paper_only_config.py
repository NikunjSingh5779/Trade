import pytest
from pydantic import ValidationError

from app.config import Settings


def test_paper_only_is_mandatory():
    with pytest.raises(ValidationError):
        Settings(live_trading_enabled=True)


def test_hosted_fallback_requires_explicit_eligibility_and_credentials():
    with pytest.raises(ValidationError):
        Settings(hosted_fallback_enabled=True)
