from fractions import Fraction

from detector.response import build_response, evaluate_response
from detector.trace import TraceResult
from plans.assignments import RollAssignment
from plans.revisions import PlanRevision
from recording.ingest import OriginalTick


ASSIGNMENT = RollAssignment("roll-retest", PlanRevision("surface", "r1", "du", 5))
DEVIATIONS = {
    "short-a": (5, -1),
    "short-b": (5, -2),
    "middle": (4, 0, 2),
    "tail": (0, 2),
}
WORKED = (
    # recording, tick, replacement, final amount, required, forbidden, rendered pieces
    ("short-a", 1, (3, 3), (2, 2), (), (), ((3, 3, True, True, (), 1, -1),)),
    ("short-b", 1, (3, 3), (2, 2), (), (), ()),
    ("middle", 2, (-6, 3), (2, 2), (), (),
     ((-6, -4, True, True, (), 0, 2), (1, 3, True, True, (2,), 0, 2))),
    ("middle", 2, (-6, 3), (0, 0), (3,), (2,), ((-1, 1, True, False, (3,), 0, 0),)),
    ("middle", 2, (-6, 3), (Fraction(5, 2), Fraction(7, 2)), (), (),
     ((-Fraction(7, 2), -Fraction(5, 2), True, True, (), 1, 6),)),
    ("tail", 2, (0, 4), (3, 3), (), (), ((3, 3, True, True, (), 1, 0),)),
)


def ordinary_responses():
    responses = {}
    for name in ("short-a", "short-b"):
        ticks = tuple(OriginalTick(tick, value) for tick, value in enumerate(DEVIATIONS[name], 1))
        response = build_response(ticks, ASSIGNMENT.plan.threshold)
        result = evaluate_response(response, 0)
        if result != TraceResult((1,), Fraction(0)):
            raise RuntimeError(f"Unexpected V1 response for {name}: {result!r}")
        print(f"{name} V1 alarms={result.alarms} amount={result.amount}")
        responses[name] = response
    if responses["short-a"] != responses["short-b"]:
        raise RuntimeError("Ordinary responses differ for the collision example")


def main():
    ordinary_responses()


if __name__ == "__main__":
    main()
