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


def main():
    ordinary = {}
    for name in ("short-a", "short-b"):
        ticks = tuple(OriginalTick(tick, value) for tick, value in enumerate(DEVIATIONS[name], 1))
        response = build_response(ticks, ASSIGNMENT.plan.threshold)
        ordinary[name] = response
        result = evaluate_response(response, 0)
        if result != TraceResult((1,), Fraction(0)):
            raise RuntimeError(f"Unexpected V1 response for {name}: {result!r}")
        print(f"{name} V1 alarms={result.alarms} amount={result.amount}")
    if ordinary["short-a"] != ordinary["short-b"]:
        raise RuntimeError("Ordinary responses differ for the collision example")


if __name__ == "__main__":
    main()
