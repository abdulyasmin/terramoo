import pytest

from terramoo.cli import parse_object_arg
from terramoo.errors import MooError
from terramoo.moolit import Obj


@pytest.mark.parametrize("text, obj", [
    ("#123", Obj(123)),
    ("123", Obj(123)),
    ("#048D05-1234567890", Obj("048D05-1234567890")),
])
def test_object_argument(text, obj):
    assert parse_object_arg(text) == obj


@pytest.mark.parametrize("text", ["hall", "#", "#12x", "$room"])
def test_bad_object_argument_is_a_moo_error(text):
    with pytest.raises(MooError):
        parse_object_arg(text)
