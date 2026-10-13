class MooError(RuntimeError):
    """The MOO refused (an E_* error, a compile error) or could not be reached."""


class AnswerError(MooError):
    """The MOO answered, but terramoo cannot read the value it sent."""
