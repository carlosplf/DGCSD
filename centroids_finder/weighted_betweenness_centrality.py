import heapq
import logging
import networkx as nx
from torch_geometric.utils import to_networkx
from utils import distance_calculator


def select_centroids(data, Z, n_clusters):
    logging.info("Using Weighted Betweenness Centrality to find the centroids...")
    G = nx.Graph(to_networkx(data))
  
    # Calculating diatances via COSINE SIMILARITY. 1 = equal, 0 = very different.
    distances = distance_calculator.graph_attr_distances(Z.detach().numpy(), mechanism="cosine")

    g_attrs = distance_calculator.define_weights(
        G=G, distances=distances, weight_name="distancia", multiplier="inverse"
    )

    nx.set_edge_attributes(G, g_attrs)
    
    # Get a sample of weights, just to check.
    edges_weights = list(nx.get_edge_attributes(G, "distancia").items())[:3]
    msg = "Edge weights sample: " + str(edges_weights)
    logging.info(msg)
   
    # Weights are used to calculate weighted shortest paths, so they are interpreted as distances
    bc_nodes = nx.betweenness_centrality(G, weight="distancia")

    biggest = heapq.nlargest(n_clusters, bc_nodes.items(), key=lambda i: i[1])
    centroids = [i[0] for i in biggest]

    clusters_centroids = []

    # Get Z values for each centroid.
    for c in centroids:
        clusters_centroids.append(Z[c].tolist())

    return clusters_centroids
