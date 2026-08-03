from app.graph.checkpointer import _checkpoint_serializer
from app.models.review import ReviewPhase


def test_checkpoint_serializer_allows_review_phase_explicitly() -> None:
    serializer = _checkpoint_serializer()

    encoded = serializer.dumps_typed(ReviewPhase.prepare)

    assert serializer.loads_typed(encoded) == ReviewPhase.prepare
