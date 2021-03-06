import argparse
import torch
import torch.utils.data
from sklearn.model_selection import ParameterGrid
from torch import nn, optim
from torch.nn import functional as F
from torch_geometric.data import DataLoader
from torch_geometric.utils import to_dense_batch
from torchvision import datasets, transforms
from torchvision.utils import save_image
import numpy as np

from datasets import BrainDataset
from utils import ConnType, ConvStrategy, Normalisation, PoolingStrategy, create_name_for_brain_dataset, \
    StratifiedGroupKFold, merge_y_and_others, create_name_for_encoder_model, create_best_encoder_name, EncodingStrategy


class VAE(nn.Module):
    def __init__(self):
        super(VAE, self).__init__()

        self.MODEL_NAME = '3layerVAE'
        self.MODEL_VERSION = 1.0
        self.EMBED_SIZE = 50
        self.activation = nn.Tanh()


        self.fc12 = nn.Linear(1200, 600)
        self.fc23 = nn.Linear(600, 300)
        self.fc3_mean = nn.Linear(300, self.EMBED_SIZE)
        self.fc3_logvar = nn.Linear(300, self.EMBED_SIZE)

        self.fc_repr_3 = nn.Linear(self.EMBED_SIZE, 300)
        self.fc32 = nn.Linear(300, 600)
        self.fc21 = nn.Linear(600, 1200)

    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5 * logvar)
        eps = torch.randn_like(std)
        return mu + eps * std

    def encode(self, x):
        h1 = self.activation(self.fc12(x))
        h2 = self.activation(self.fc23(h1))
        return self.fc3_mean(h2), self.fc3_logvar(h2)

    def decode(self, z):
        h2 = self.activation(self.fc_repr_3(z))
        h4 = self.activation(self.fc32(h2))
        return self.fc21(h4)

    def forward(self, x):
        mu, logvar = self.encode(x)
        z = self.reparameterize(mu, logvar)
        return self.decode(z), mu, logvar

    def to_string_name(self):
        return self.MODEL_NAME + '_' + str(self.MODEL_VERSION)



class AE(nn.Module):
    def __init__(self):
        super(AE, self).__init__()

        self.MODEL_NAME = '3layerAE'
        self.MODEL_VERSION = 1.0
        self.EMBED_SIZE = 50
        self.activation = nn.Tanh()


        self.fc12 = nn.Linear(1200, 600)
        self.fc23 = nn.Linear(600, 300)
        self.fc34 = nn.Linear(300, self.EMBED_SIZE)

        self.fc43 = nn.Linear(self.EMBED_SIZE, 300)
        self.fc32 = nn.Linear(300, 600)
        self.fc21 = nn.Linear(600, 1200)


    def encode(self, x):
        h1 = self.activation(self.fc12(x))
        h2 = self.activation(self.fc23(h1))
        return self.fc34(h2)

    def decode(self, z):
        h2 = self.activation(self.fc43(z))
        h4 = self.activation(self.fc32(h2))
        return self.fc21(h4)

    def forward(self, x):
        z = self.encode(x)
        return self.decode(z)

    def to_string_name(self):
        return self.MODEL_NAME + '_' + str(self.MODEL_VERSION)

def loss_function_ae(recon_x, x):#, mu, logvar):
    reconstruction_loss = F.smooth_l1_loss(recon_x, x, reduction='sum')

    return reconstruction_loss

# Reconstruction + KL divergence losses summed over all elements and batch
def loss_function_vae(recon_x, x, mu, logvar):
    #BCE = F.binary_cross_entropy(recon_x, x.view(-1, 784), reduction='sum')
    reconstruction_loss = F.smooth_l1_loss(recon_x, x, reduction='sum')

    # see Appendix B from VAE paper:
    # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
    # https://arxiv.org/abs/1312.6114
    # 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
    kdl_loss = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())

    return reconstruction_loss + kdl_loss


def train_model(model, train_loader):
    model.train()
    loss_all = 0

    for data in train_loader:
        data_ts = data.x.to(device)
        optimizer.zero_grad()

        if ENCODING_STRATEGY == EncodingStrategy.AE3layers:
            reconstructed_batch = model(data_ts)
            loss = loss_function_ae(reconstructed_batch, data_ts)
        elif ENCODING_STRATEGY == EncodingStrategy.VAE3layers:
            reconstructed_batch, mu, logvar = model(data_ts)
            loss = loss_function_vae(reconstructed_batch, data_ts, mu, logvar)

        loss.backward()
        loss_all += loss.item()
        optimizer.step()

    # len(train_loader) gives the number of batches
    # len(train_loader.dataset) gives the number of graphs
    return loss_all / (len(train_loader.dataset) * NUM_NODES)


def evaluate_model(model, loader, save_comparison=False):
    model.eval()
    loss_all = 0

    for batch_id, data in enumerate(loader):
        data_ts = data.x.to(device)

        if ENCODING_STRATEGY == EncodingStrategy.AE3layers:
            reconstructed_batch = model(data_ts)
            loss = loss_function_ae(reconstructed_batch, data_ts)
        elif ENCODING_STRATEGY == EncodingStrategy.VAE3layers:
            reconstructed_batch, mu, logvar = model(data_ts)
            loss = loss_function_vae(reconstructed_batch, data_ts, mu, logvar)

        loss_all += loss.item()

        if save_comparison:
            orig_ts, batch_bool = to_dense_batch(data_ts, data.batch.to(device))
            recons_ts, _ = to_dense_batch(reconstructed_batch, data.batch.to(device))
            sav_name = ENCODING_STRATEGY.value

            for id in range(len(batch_bool)):
                np.save(arr=orig_ts[id].cpu().numpy(), file=f'encoder_comparisons/{sav_name}_{SPLIT_TO_TEST}_{batch_id}_{id}_orig.npy')
                np.save(arr=recons_ts[id].detach().cpu().numpy(), file=f'encoder_comparisons/{sav_name}_{SPLIT_TO_TEST}_{batch_id}_{id}_recons.npy')

    # len(train_loader) gives the number of batches
    # len(train_loader.dataset) gives the number of graphs
    return loss_all / (len(loader.dataset) * NUM_NODES)


def training_step(outer_split_no, inner_split_no, epoch, model, train_loader, val_loader):
    _ = train_model(model, train_loader)
    train_loss = evaluate_model(model, train_loader)
    val_loss = evaluate_model(model, val_loader)


    print(f'{outer_split_no}-{inner_split_no}-Epoch: {epoch}, Loss: {round(train_loss, 5)} / {round(val_loss, 5)}')

    return train_loss, val_loss

if __name__ == "__main__":
    torch.manual_seed(1)
    np.random.seed(1111)

    parser = argparse.ArgumentParser()

    parser.add_argument("--device", default="cuda")

    parser.add_argument("--fold_num", type=int, default=1)
    parser.add_argument("--target_var", default='gender')
    parser.add_argument("--activation", default='relu')
    parser.add_argument("--threshold", default=5, type=int)
    parser.add_argument("--num_nodes", default=50, type=int)
    parser.add_argument("--num_epochs", default=50, type=int)
    parser.add_argument("--batch_size", default=1000, type=int)
    parser.add_argument("--add_gcn", type=bool, default=False)  # to make true just include flag with 1
    parser.add_argument("--add_gat", type=bool, default=False)  # to make true just include flag with 1
    parser.add_argument("--remove_disconnected_nodes", type=bool,
                        default=False)  # to make true just include flag with 1
    parser.add_argument("--conn_type", default="fmri")
    parser.add_argument("--conv_strategy", default="entire")
    parser.add_argument("--pooling",
                        default="mean")  # 2) Try other pooling mechanisms CONCAT (only with fixed num_nodes across graphs),
    parser.add_argument("--channels_conv", type=int, default=8)
    parser.add_argument("--normalisation", default='roi_norm')
    parser.add_argument("--time_length", type=int, default=1200)
    parser.add_argument("--encoding_strategy", default='none')

    args = parser.parse_args()

    device = torch.device(args.device)

    # Making a single variable for each argument
    N_EPOCHS = args.num_epochs
    TARGET_VAR = args.target_var
    ACTIVATION = args.activation
    THRESHOLD = args.threshold
    SPLIT_TO_TEST = args.fold_num
    ADD_GCN = args.add_gcn
    ADD_GAT = args.add_gat
    BATCH_SIZE = args.batch_size
    REMOVE_NODES = args.remove_disconnected_nodes
    NUM_NODES = args.num_nodes
    CONN_TYPE = ConnType(args.conn_type)
    CONV_STRATEGY = ConvStrategy(args.conv_strategy)
    POOLING = PoolingStrategy(args.pooling)
    CHANNELS_CONV = args.channels_conv
    NORMALISATION = Normalisation(args.normalisation)
    TIME_LENGTH = args.time_length
    TS_SPIT_NUM = int(4800 / TIME_LENGTH)
    ENCODING_STRATEGY = EncodingStrategy(args.encoding_strategy)
    print("Encoding strategy is:", ENCODING_STRATEGY)

    name_dataset = create_name_for_brain_dataset(num_nodes=NUM_NODES,
                                                 time_length=TIME_LENGTH,
                                                 target_var=TARGET_VAR,
                                                 threshold=THRESHOLD,
                                                 normalisation=NORMALISATION,
                                                 connectivity_type=CONN_TYPE,
                                                 disconnect_nodes=REMOVE_NODES)
    print("Going for", name_dataset)
    dataset = BrainDataset(root=name_dataset,
                           time_length=TIME_LENGTH,
                           num_nodes=NUM_NODES,
                           target_var=TARGET_VAR,
                           threshold=THRESHOLD,
                           normalisation=NORMALISATION,
                           connectivity_type=CONN_TYPE,
                           disconnect_nodes=REMOVE_NODES)

    N_OUT_SPLITS = 5
    N_INNER_SPLITS = 5

    # Stratification will occur with regards to both the sex and session day
    skf = StratifiedGroupKFold(n_splits=N_OUT_SPLITS, random_state=1111)
    merged_labels = merge_y_and_others(dataset.data.y,
                                       dataset.data.index)
    skf_generator = skf.split(np.zeros((len(dataset), 1)),
                              merged_labels,
                              groups=dataset.data.hcp_id.tolist())

    #
    # Main outer-loop
    #
    outer_split_num = 0
    for train_index, test_index in skf_generator:
        outer_split_num += 1

        # Only run for the specific fold defined in the script arguments.
        if outer_split_num != SPLIT_TO_TEST:
            continue

        X_train_out = dataset[torch.tensor(train_index)]
        X_test_out = dataset[torch.tensor(test_index)]

        print("Size is:", len(X_train_out), "/", len(X_test_out))
        print("Positive classes:", sum(X_train_out.data.y.numpy()), "/", sum(X_test_out.data.y.numpy()))

        train_out_loader = DataLoader(X_train_out, batch_size=BATCH_SIZE, shuffle=True)
        test_out_loader = DataLoader(X_test_out, batch_size=BATCH_SIZE, shuffle=False)

        param_grid = {'weight_decay': [0],
                      'lr': [1e-3]
                      }

        grid = ParameterGrid(param_grid)
        best_model_name_outer_fold_loss = None
        best_outer_metric_loss = 1000
        for params in grid:
            print("For ", params)

            skf_inner = StratifiedGroupKFold(n_splits=N_INNER_SPLITS, random_state=1111)
            merged_labels_inner = merge_y_and_others(X_train_out.data.y,
                                                     X_train_out.data.index)
            skf_inner_generator = skf_inner.split(np.zeros((len(X_train_out), 1)),
                                                  merged_labels_inner,
                                                  groups=X_train_out.data.hcp_id.tolist())
            model_with_sigmoid = True

            # This for-cycle will only be executed once (for now)
            for inner_train_index, inner_val_index in skf_inner_generator:

                if ENCODING_STRATEGY == EncodingStrategy.AE3layers:
                    model = AE().to(device)
                elif ENCODING_STRATEGY == EncodingStrategy.VAE3layers:
                    model = VAE().to(device)
                trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
                print("Number of trainable params:", trainable_params)

                X_train_in = X_train_out[torch.tensor(inner_train_index)]
                X_val_in = X_train_out[torch.tensor(inner_val_index)]

                train_in_loader = DataLoader(X_train_in, batch_size=BATCH_SIZE, shuffle=True)
                val_loader = DataLoader(X_val_in, batch_size=BATCH_SIZE, shuffle=False)

                optimizer = torch.optim.Adam(model.parameters(),
                                             lr=params['lr'],
                                             weight_decay=params['weight_decay'])

                inner_model_name = create_name_for_encoder_model(ts_length=TIME_LENGTH,
                                                           outer_split_num=outer_split_num,
                                                           encoder_name=model.to_string_name(),
                                                           params=params)
                loss_history_path = create_name_for_encoder_model(ts_length=TIME_LENGTH,
                                                                  outer_split_num=outer_split_num,
                                                                  encoder_name=model.to_string_name(),
                                                                  params=params,
                                                                  suffix='')

                best_metrics_fold_loss = 1000
                losses = {'train' : [],
                          'val': []}
                for epoch in range(N_EPOCHS):
                    train_loss, val_loss = training_step(outer_split_num,
                                                  0,
                                                  epoch,
                                                  model,
                                                  train_in_loader,
                                                  val_loader)
                    losses['train'].append(train_loss)
                    losses['val'].append(val_loss)

                    if val_loss < best_metrics_fold_loss:
                        best_metrics_fold_loss = val_loss
                        torch.save(model, inner_model_name)
                        if val_loss < best_outer_metric_loss:
                            best_outer_metric_loss = val_loss
                            best_model_name_outer_fold_loss = inner_model_name
                np.save(arr=np.array(losses['train']), file=loss_history_path + '_train.npy')
                np.save(arr=np.array(losses['val']), file=loss_history_path + '_val.npy')
                break

        model = torch.load(best_model_name_outer_fold_loss)
        test_loss = evaluate_model(model, test_out_loader, save_comparison=True)
        print('Best params: ', best_model_name_outer_fold_loss, '(', best_outer_metric_loss, ')')
        print(f'{outer_split_num}--Final Loss: {round(test_loss, 5)}')

        torch.save(model, create_best_encoder_name(ts_length=TIME_LENGTH,
                                                   outer_split_num=outer_split_num,
                                                   encoder_name=model.MODEL_NAME))
