import logging
import random


def select_centroids(Z, n_clusters):
    logging.info("Using Random Seeds to find the centroids...")

    centroids = random.sample(range(1, len(Z)), n_clusters)

    clusters_centroids = []

    # Get Z values for each centroid.
    for c in centroids:
        clusters_centroids.append(Z[c].tolist())

    return clusters_centroids
