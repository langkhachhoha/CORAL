import random
import math

random.seed(42)
CITIES = [(random.random(), random.random()) for _ in range(100)]

def distance(a, b):
    return math.sqrt((CITIES[a][0] - CITIES[b][0])**2 + (CITIES[a][1] - CITIES[b][1])**2)

def total_distance(tour):
    return sum(distance(tour[i], tour[(i+1) % len(tour)]) for i in range(len(tour)))

def nearest_neighbor(start=0):
    """Build initial tour using nearest neighbor heuristic."""
    visited = [False] * len(CITIES)
    tour = [start]
    visited[start] = True
    for _ in range(len(CITIES) - 1):
        current = tour[-1]
        nearest = min((i for i in range(len(CITIES)) if not visited[i]),
                      key=lambda i: distance(current, i))
        tour.append(nearest)
        visited[nearest] = True
    return tour

def two_opt(tour):
    """Improve tour using 2-opt swaps."""
    improved = True
    while improved:
        improved = False
        for i in range(len(tour)):
            for j in range(i + 2, len(tour)):
                if j == len(tour) - 1 and i == 0:
                    continue
                a, b = tour[i], tour[(i+1) % len(tour)]
                c, d = tour[j], tour[(j+1) % len(tour)]
                old_dist = distance(a, b) + distance(c, d)
                new_dist = distance(a, c) + distance(b, d)
                if new_dist < old_dist - 1e-10:
                    tour[i+1:j+1] = tour[i+1:j+1][::-1]
                    improved = True
    return tour

# Start with nearest neighbor from all possible starting points, then improve with 2-opt
best_tour = None
best_dist = float('inf')
for start in range(len(CITIES)):
    tour = nearest_neighbor(start)
    tour = two_opt(tour)
    dist = total_distance(tour)
    if dist < best_dist:
        best_dist = dist
        best_tour = tour

# Try a few more random restarts
import time
random.seed(int(time.time() * 1000) % 2**32)
for _ in range(30):
    start = random.randint(0, len(CITIES) - 1)
    tour = nearest_neighbor(start)
    tour = two_opt(tour)
    dist = total_distance(tour)
    if dist < best_dist:
        best_dist = dist
        best_tour = tour

for city in best_tour:
    print(city)
