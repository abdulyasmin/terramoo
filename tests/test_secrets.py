import pytest

from terramoo.errors import MooError
from terramoo.secrets import check_secret


def test_plain_secret_is_stripped():
    assert check_secret("  abc.DEF-123\n") == "abc.DEF-123"


@pytest.mark.parametrize("pasted", ['{"token": ', '"abc"', "'abc'", "[1]", "", "   "])
def test_json_quotes_and_empty_are_refused(pasted):
    with pytest.raises(MooError):
        check_secret(pasted)
