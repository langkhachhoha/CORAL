import math
import random
from coral.grader import TaskGrader
from coral.types import ScoreBundle

# keep consistent with the problem statement in `solution.py`
random.seed(42)
CITIES = [(random.random(), random.random()) for _ in range(100)]

class Grader(TaskGrader):
    def evaluate(self) -> float | ScoreBundle:
        try:
            result = self.run_program("solution.py")  # runs solution.py, returns CompletedProcess
            order = [int(x) for x in result.stdout.strip().split("\n")]
            assert sorted(order) == list(range(len(CITIES)))
            dist = sum(
                math.dist(CITIES[order[i]], CITIES[order[(i + 1) % len(order)]])
                for i in range(len(order))
            )
            return -dist  # shorter tour = higher score
        except Exception as e:
            return self.fail(str(e))  # records failure and returns null score
