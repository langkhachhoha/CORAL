import random

# Restate the problem here as the agent cannot read the content of `grader.py`
random.seed(42)
CITIES = [(random.random(), random.random()) for _ in range(100)]

# Naive: visit cities in index order (0, 1, 2, ..., 99)
for i in range(len(CITIES)):
    print(i)
