import warnings
import torch
import numpy as np
import logging
import time
from torch_geometric.nn import GAE
from gat_model import gat_model
from sklearn.metrics.cluster import normalized_mutual_info_score, adjusted_rand_score
from utils import clustering_loss, csv_writer, plot_functions
from metrics import modularity
from centroids_finder import arguments_map

# Ignore torch FutureWarning messages
warnings.simplefilter(action="ignore", category=FutureWarning)

# Global hyperparameters
LEARNING_RATE = 0.0001              # Learning rate for the GAE model
LR_CHANGE_GAMMA = 0.9               # Learning rate decay factor
LR_CHANGE_EPOCHS = 100              # Number of epochs between LR updates
UPDATE_CLUSTERS_STEP_SIZE = 0.001   # Step size for updating cluster centroids
RECHOSE_CENTROIDS = True            # Re-choose centroids if loss isn’t improving
NOT_IMPROVING_LIMIT = 100           # Max epochs of non-improvement before re-choosing centroids


class GaeRunner:
    def __init__(self, epochs, data, b_edge_index, n_clusters, find_centroids_alg, 
                 c_loss_gama, p_interval, centroids_plot_file, clustering_plot_file,
                 loss_log_file, metrics_log_file, hidden_layer, output_layer):
        self.epochs = epochs
        self.data = data
        self.b_edge_index = b_edge_index
        self.n_clusters = n_clusters
        self.find_centroids_alg = find_centroids_alg
        self.c_loss_gama = c_loss_gama
        self.p_interval = p_interval
        self.centroids_plot_file = centroids_plot_file
        self.clustering_plot_file = clustering_plot_file
        self.loss_log_file = loss_log_file
        self.metrics_log_file = metrics_log_file
        self.hidden_layer_size = hidden_layer
        self.output_layer_size = output_layer

        # Internal state variables
        self.Q = None
        self.P = None
        self.clusters_centroids = None
        self.first_interaction = True

    def __print_values(self):
        logging.info(f"C_LOSS_GAMMA: {self.c_loss_gama}")
        logging.info(f"LEARNING_RATE: {LEARNING_RATE}")
        logging.info(f"CALC_P_INTERVAL: {self.p_interval}")
        logging.info(f"LR_CHANGE_GAMMA: {LR_CHANGE_GAMMA}")
        logging.info(f"LR_CHANGE_EPOCHS: {LR_CHANGE_EPOCHS}")

    def _initialize_model(self, device):
        """
        Initializes the GAE model, optimizer, and learning rate scheduler.
        """
        in_channels = self.data.x.shape[1]
        hidden_channels = self.hidden_layer_size
        out_channels = self.output_layer_size

        # Create the multilayer GAT (recommended configuration for Cora)
        gat = gat_model.MultiLayerGAT(
            in_channels=in_channels,
            hidden_channels=hidden_channels,
            out_channels=out_channels,
            num_layers=2,       # One hidden layer + output layer
            heads=16,           # Number of attention heads in the hidden layer
            dropout=0.2,        # Dropout rate
            concat=True         # Concatenate outputs of the attention heads in the hidden layer
        )
        # Wrap the GAT in the GAE model
        gae = GAE(gat)
        gae = gae.float().to(device)

        # Move data to device
        self.data = self.data.to(device)
        self.b_edge_index = self.b_edge_index.to(device)

        optimizer = torch.optim.Adam(gae.parameters(), lr=LEARNING_RATE)
        scheduler = torch.optim.lr_scheduler.StepLR(
            optimizer, step_size=LR_CHANGE_EPOCHS, gamma=LR_CHANGE_GAMMA
        )
        return gae, optimizer, scheduler

    def run_training(self):
        """
        Main training loop. Sets up the model, runs through the epochs,
        collects metrics, and saves logs/plots.
        """
        self.__print_values()

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        logging.info(f"Running on {device}")

        gae, optimizer, scheduler = self._initialize_model(device)

        # Containers for logging and metrics
        loss_log = []
        metrics_log = []
        best_nmi = {"epoch": 0, "value": 0.0}
        best_ari = {"epoch": 0, "value": 0.0}
        best_mod = {"epoch": 0, "value": 0.0}
        loss_not_improving_counter = 0
        past_loss = float("inf")
        att_tuple = None

        # Epoch training loop
        for epoch in range(self.epochs):
            # Re-choose centroids if first epoch or if loss has not improved for a while
            chose_centroids = (epoch == 0 or loss_not_improving_counter >= NOT_IMPROVING_LIMIT) and RECHOSE_CENTROIDS
            if chose_centroids:
                loss_not_improving_counter = 0

            total_loss, Z, att_tuple, clustering_loss_val, gae_loss_val = self.__train_epoch(
                gae, optimizer, scheduler, epoch, chose_centroids
            )

            # Update non-improvement counter
            if total_loss >= past_loss:
                loss_not_improving_counter += 1
            else:
                loss_not_improving_counter = 0
            past_loss = total_loss

            logging.info(f"Epoch {epoch} - Total Loss: {total_loss}")
            loss_log.append([epoch, total_loss, clustering_loss_val, gae_loss_val])

            # Compute and log clustering metrics
            metrics = self._compute_metrics()
            metrics_log.append([epoch, metrics["mod"], metrics["nmi"], metrics["ari"]])
            logging.info(f"Epoch {epoch} - Modularity: {metrics['mod']}, NMI: {metrics['nmi']}, ARI: {metrics['ari']}")

            # Update best scores
            if metrics["nmi"] > best_nmi["value"]:
                best_nmi = {"epoch": epoch, "value": metrics["nmi"]}
            if metrics["ari"] > best_ari["value"]:
                best_ari = {"epoch": epoch, "value": metrics["ari"]}
            if metrics["mod"] > best_mod["value"]:
                best_mod = {"epoch": epoch, "value": metrics["mod"]}

            # Plot clustering results for this epoch
            # if epoch % 5 == 0:
            #     clustering_filename = f"{self.clustering_plot_file[:-4]}_{epoch}.png"
            #    plot_functions.plot_clustering(Z.detach().cpu().numpy(), metrics["r"], clustering_filename)

        # Log best metrics
        logging.info(f"Best Modularity: {best_mod['value']} at epoch {best_mod['epoch']}")
        logging.info(f"Best NMI: {best_nmi['value']} at epoch {best_nmi['epoch']}")
        logging.info(f"Best ARI: {best_ari['value']} at epoch {best_ari['epoch']}")

        # Write logs to CSV files
        csv_writer.write_loss(loss_log, self.loss_log_file)
        csv_writer.write_metrics(metrics_log, self.metrics_log_file)

        return self.data, att_tuple

    def __train_epoch(self, gae, optimizer, scheduler, epoch, chose_centroids):
        """
        Trains the model for one epoch.
        Returns:
            total_loss (float): Combined loss (reconstruction + clustering)
            Z (torch.Tensor): Embeddings from the encoder.
            att_tuple: Attention outputs from the model.
            clustering_loss_val (float): The clustering (KL divergence) loss.
            gae_loss_val (float): The reconstruction loss.
        """
        gae.train()
        optimizer.zero_grad()

        # 1. Forward pass to obtain embeddings and attention tuple.
        att_tuple, Z = self._forward_pass(gae)

        # 2. Handle centroid selection if required.
        self._handle_centroids(Z, chose_centroids, epoch)
        
        # Check if centroids exist, otherwise abort the epoch.
        if self.clusters_centroids is None:
            logging.error("Centroids must be chosen first. Aborting epoch.")
            return 0, Z, att_tuple, 0, 0

        # 3. Compute losses.
        total_loss, clustering_loss_tensor, gae_loss_tensor = self._compute_losses(gae, Z, epoch)

        # 4. Backpropagation and update parameters.
        total_loss.backward()
        self._update_centroids(clustering_loss_tensor)
        optimizer.step()
        scheduler.step()
        self.first_interaction = False

        return (
            total_loss.item(),
            Z,
            att_tuple,
            clustering_loss_tensor.item(),
            gae_loss_tensor.item(),
        )

    def _forward_pass(self, gae):
        """
        Performs the forward pass using the encoder.
        Returns:
            att_tuple: Attention outputs from the encoder.
            Z (torch.Tensor): Embeddings produced by the encoder.
        """
        att_tuple, Z = gae.encode(
            self.data.x.float(),
            self.b_edge_index.edge_index,
            self.b_edge_index.edge_attr,
        )
        return att_tuple, Z

    def _handle_centroids(self, Z, chose_centroids, epoch):
        """
        Selects centroids if required and plots them.
        """
        if chose_centroids:
            self._find_centroids(Z)
            plot_functions.plot_centroids(Z, self.clusters_centroids, self.centroids_plot_file)

    def _compute_losses(self, gae, Z, epoch):
        """
        Computes the clustering and reconstruction losses.
        Returns:
            total_loss: Combined loss.
            clustering_loss_tensor: Loss from the clustering (KL divergence).
            gae_loss_tensor: Reconstruction loss.
        """
        # Compute soft assignments and update target distribution periodically.
        self.Q = clustering_loss.calculate_q(self.clusters_centroids, Z)
        if epoch % self.p_interval == 0:
            self.P = clustering_loss.calculate_p(self.Q)

        clustering_loss_tensor, Q, P = clustering_loss.kl_div_loss(self.Q, self.P)
        gae_loss_tensor = gae.recon_loss(Z, self.data.edge_index)
        total_loss = gae_loss_tensor + self.c_loss_gama * clustering_loss_tensor

        return total_loss, clustering_loss_tensor, gae_loss_tensor

    def _update_centroids(self, clustering_loss_tensor):
        """
        Updates the centroids using a gradient descent step if applicable.
        """
        if not self.first_interaction and clustering_loss_tensor.item() != 0:
            with torch.no_grad():
                self.clusters_centroids -= UPDATE_CLUSTERS_STEP_SIZE * self.clusters_centroids.grad
                # Detach and set requires_grad for the next update.
                self.clusters_centroids = self.clusters_centroids.detach().clone().requires_grad_()

    def _compute_metrics(self):
        """
        Computes clustering metrics based on the current soft assignments Q.
        Returns a dictionary with keys:
            'r'   : cluster assignments per data point,
            'mod' : modularity score,
            'nmi' : normalized mutual information,
            'ari' : adjusted Rand index.
        """
        # Vectorized computation of hard cluster assignments from Q
        r = torch.argmax(self.Q, dim=1).cpu().numpy()
        mod_score = modularity.calculate(self.data, r)
        nmi_score = normalized_mutual_info_score(self.data.y.tolist(), r)
        ari_score = adjusted_rand_score(self.data.y.tolist(), r)
        return {"r": r, "mod": mod_score, "nmi": nmi_score, "ari": ari_score}

    def _find_centroids(self, Z):
        """
        Finds cluster centroids using the selected algorithm.
        The selected method from arguments_map is responsible for computing the centroids.
        """
        start = time.time()
        if self.find_centroids_alg not in arguments_map.map:
            logging.error("FIND_CENTROIDS_ALG not known. Aborting centroid selection.")
            return

        centroids = arguments_map.map[self.find_centroids_alg].select_centroids(
            data=self.data, Z=Z, n_clusters=self.n_clusters
        )
        self.clusters_centroids = torch.tensor(centroids, dtype=torch.float32, requires_grad=True)
        done = time.time()
        logging.info(f"Finished centroid finding in {done - start:.2f} seconds.")
