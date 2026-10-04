from __future__ import annotations

import pytest

from core.config import DomainSettings
from core.pipeline import Domain
from core.registry import available_domains, load_domain, load_domain_settings


def test_sql_domain_is_registered() -> None:
    assert "sql" in available_domains()
    domain = load_domain("sql")
    assert isinstance(domain, Domain)
    assert domain.name == "sql"


def test_domain_from_another_package() -> None:
    # A domain is found by convention alone -- nothing in the harness names it.
    domain = load_domain("toy", package="tests.fixtures")
    assert domain.name == "toy"
    assert available_domains("tests.fixtures") == ["toy"]


def test_settings_load_without_the_pipeline() -> None:
    settings = load_domain_settings("toy", package="tests.fixtures")
    assert isinstance(settings, DomainSettings)
    assert settings.scratch_url == "http://toy-scratch"


def test_unknown_domain() -> None:
    with pytest.raises(LookupError, match="available"):
        load_domain("no_such_domain")
    with pytest.raises(LookupError, match="available"):
        load_domain_settings("no_such_domain")


@pytest.mark.parametrize("name", ["", "sql.domain", "../etc", "two words"])
def test_invalid_name(name: str) -> None:
    with pytest.raises(ValueError, match="invalid domain name"):
        load_domain(name)
