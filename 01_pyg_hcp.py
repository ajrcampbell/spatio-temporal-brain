import argparse
import datetime
import os
import pickle
import random
import time
from collections import deque
from sys import exit

import numpy as np
import torch
import wandb
from sklearn.metrics import roc_auc_score, accuracy_score, f1_score, classification_report
from sklearn.model_selection import ParameterGrid, StratifiedKFold
from sklearn.preprocessing import LabelEncoder
from torch_geometric.data import DataLoader
from xgboost import XGBClassifier

from datasets import BrainDataset, create_hcp_correlation_vals, create_ukb_corrs_flatten
from model import SpatioTemporalModel
from utils import create_name_for_brain_dataset, create_name_for_model, Normalisation, ConnType, ConvStrategy, \
    StratifiedGroupKFold, PoolingStrategy, AnalysisType, merge_y_and_others, EncodingStrategy, create_best_encoder_name
from wandb_utils import SWEEP_GENERAL


def train_classifier(model, train_loader):
    model.train()
    loss_all = 0
    criterion = torch.nn.BCELoss()

    grads = {'final_l': [],
             'conv1d_1': []
             }
    for data in train_loader:
        data = data.to(device)
        optimizer.zero_grad()
        if POOLING == PoolingStrategy.DIFFPOOL:
            output_batch, link_loss, ent_loss = model(data)
            loss = criterion(output_batch, data.y.unsqueeze(1)) + link_loss + ent_loss
        else:
            output_batch = model(data)
            loss = criterion(output_batch, data.y.unsqueeze(1))

        loss.backward()

        grads['final_l'].extend(model.final_linear.weight.grad.flatten().cpu().tolist())
        grads['conv1d_1'].extend(model.final_linear.weight.grad.flatten().cpu().tolist())

        loss_all += loss.item() * data.num_graphs
        optimizer.step()
    print("GRAD", np.mean(grads['final_l']), np.std(grads['final_l']))
    # len(train_loader) gives the number of batches
    # len(train_loader.dataset) gives the number of graphs

    # Returning a weighted average according to number of graphs
    return loss_all / len(train_loader.dataset)


def return_metrics(labels, pred_binary, pred_prob, loss_value=None):
    roc_auc = roc_auc_score(labels, pred_prob)
    acc = accuracy_score(labels, pred_binary)
    f1 = f1_score(labels, pred_binary, zero_division=0)
    report = classification_report(labels, pred_binary, output_dict=True, zero_division=0)
    sens = report['1.0']['recall']
    spec = report['0.0']['recall']

    return {'loss': loss_value,
            'auc': roc_auc,
            'acc': acc,
            'f1': f1,
            'sensitivity': sens,
            'specificity': spec
            }


def evaluate_classifier(loader, save_path_preds=None):
    model.eval()
    criterion = torch.nn.BCELoss()

    predictions = []
    labels = []
    test_error = 0

    for data in loader:
        with torch.no_grad():
            data = data.to(device)
            if POOLING == PoolingStrategy.DIFFPOOL:
                output_batch, link_loss, ent_loss = model(data)
                output_batch = output_batch.flatten()
                loss = criterion(output_batch, data.y) + link_loss + ent_loss
            else:
                output_batch = model(data)
                output_batch = output_batch.flatten()
                loss = criterion(output_batch, data.y)

            test_error += loss.item() * data.num_graphs

            pred = output_batch.detach().cpu().numpy()

            label = data.y.detach().cpu().numpy()
            predictions.append(pred)
            labels.append(label)
    predictions = np.hstack(predictions)
    labels = np.hstack(labels)

    if save_path_preds is not None:
        np.save('results/labels_' + save_path_preds, labels)
        np.save('results/predictions_' + save_path_preds, predictions)

    pred_binary = np.where(predictions > 0.5, 1, 0)

    return return_metrics(labels, pred_binary, predictions, loss_value=test_error / len(loader.dataset))


def classifier_step(outer_split_no, inner_split_no, epoch, model, train_loader, val_loader):
    loss = train_classifier(model, train_loader)
    train_metrics = evaluate_classifier(train_loader)
    val_metrics = evaluate_classifier(val_loader)

    print(
        '{:1d}-{:1d}-Epoch: {:03d}, Loss: {:.7f} / {:.7f}, Auc: {:.4f} / {:.4f}, Acc: {:.4f} / {:.4f}, F1: {:.4f} / '
        '{:.4f} '.format(outer_split_no, inner_split_no, epoch, loss, val_metrics['loss'], train_metrics['auc'],
                         val_metrics['auc'],
                         train_metrics['acc'], val_metrics['acc'], train_metrics['f1'], val_metrics['f1']))

    return val_metrics


def get_array_data(data_fold, num_nodes=50):
    tmp_array = []
    tmp_y = []

    for d in data_fold:
        if num_nodes == 376:
            tmp_array.append(flatten_correlations[d.ukb_id.item()])
        else:
            tmp_array.append(flatten_correlations[(d.hcp_id.item(), d.index.item())])
        tmp_y.append(d.y.item())

    return np.array(tmp_array), np.array(tmp_y)


def main_loop():
    # import warnings

    # warnings.filterwarnings("ignore")
    torch.manual_seed(1)
    # torch.backends.cudnn.deterministic = True
    # torch.backends.cudnn.benchmark = False
    np.random.seed(1111)
    random.seed(1111)
    torch.cuda.manual_seed_all(1111)

    # To check time execution
    start_time = time.time()

    # Device part
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
    ANALYSIS_TYPE = AnalysisType(args.analysis_type)
    TIME_LENGTH = args.time_length
    TS_SPIT_NUM = int(4800 / TIME_LENGTH)
    ENCODING_STRATEGY = EncodingStrategy(args.encoding_strategy)
    EARLY_STOP_STEPS = args.early_stop_steps

    # if CONV_STRATEGY != ConvStrategy.TCN_ENTIRE:
    #    print("Setting to deterministic runs")
    #    torch.backends.cudnn.deterministic = True
    # else:
    #    print("This run will not be deterministic")
    print("This run will not be deterministic")
    if TARGET_VAR not in ['gender']:
        print("Unrecognised target_var")
        exit(-1)
    else:
        print("Predicting", TARGET_VAR, N_EPOCHS, SPLIT_TO_TEST, ADD_GCN, ACTIVATION, THRESHOLD, ADD_GAT,
              BATCH_SIZE, REMOVE_NODES, NUM_NODES, CONN_TYPE, CONV_STRATEGY, POOLING, CHANNELS_CONV, TIME_LENGTH)

    #
    # Definition of general variables
    #
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
    if ANALYSIS_TYPE == AnalysisType.FLATTEN_CORRS:
        if NUM_NODES == 376:
            flatten_correlations = create_ukb_corrs_flatten()
        else:
            flatten_correlations = create_hcp_correlation_vals(NUM_NODES, ts_split_num=TS_SPIT_NUM)
    elif ANALYSIS_TYPE == AnalysisType.FLATTEN_CORRS_THRESHOLD:
        flatten_correlations = create_hcp_correlation_vals(NUM_NODES, ts_split_num=TS_SPIT_NUM,
                                                           binarise=True, threshold=THRESHOLD)

    N_OUT_SPLITS = 5
    N_INNER_SPLITS = 5

    # UK Biobank
    if NUM_NODES == 376:
        skf = StratifiedKFold(n_splits=N_OUT_SPLITS, shuffle=True, random_state=1111)
        skf_generator = skf.split(np.zeros((len(dataset), 1)),
                                  np.array([data.y.item() for data in dataset]))
    else:
        # Stratification will occur with regards to both the sex and session day
        skf = StratifiedGroupKFold(n_splits=N_OUT_SPLITS, random_state=1111)
        merged_labels = merge_y_and_others(torch.cat([data.y for data in dataset], dim=0),
                                           torch.cat([data.index for data in dataset], dim=0))
        skf_generator = skf.split(np.zeros((len(dataset), 1)),
                                  merged_labels,
                                  groups=[data.hcp_id.item() for data in dataset])

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
        print("Positive classes:", sum([data.y.item() for data in X_train_out]),
              "/", sum([data.y.item() for data in X_test_out]))

        train_out_loader = DataLoader(X_train_out, batch_size=BATCH_SIZE, shuffle=True)
        test_out_loader = DataLoader(X_test_out, batch_size=BATCH_SIZE, shuffle=False)
        #
        # Main inner-loop (for now, not really an inner loop - just one train/val inside
        #
        if ANALYSIS_TYPE == AnalysisType.SPATIOTEMOPRAL:
            param_grid = {'weight_decay': [0.005, 0.5, 0, 1],
                          'lr': [1e-4, 1e-5, 1e-6],
                          'dropout': [0, 0.5, 0.7]
                          }
        elif ANALYSIS_TYPE == AnalysisType.FLATTEN_CORRS or ANALYSIS_TYPE == AnalysisType.FLATTEN_CORRS_THRESHOLD:
            param_grid = {
                'min_child_weight': [1],  # , 5],
                'gamma': [0.0, 1, 5],
                'subsample': [0.6, 1.0],
                'colsample_bytree': [0.6, 1.0],
                'max_depth': [3],  # , 5],
                'n_estimators': [100, 500]
            }

        grid = ParameterGrid(param_grid)
        # best_metric = -100
        # best_params = None
        best_model_name_outer_fold_auc = None
        best_model_name_outer_fold_loss = None
        best_outer_metric_loss = 1000
        best_outer_metric_auc = -1000
        for params in grid:
            print("For ", params)

            # UK Biobank
            if NUM_NODES == 376:
                skf_inner = StratifiedKFold(n_splits=N_INNER_SPLITS, shuffle=True, random_state=1111)
                skf_inner_generator = skf_inner.split(np.zeros((len(X_train_out), 1)),
                                                      np.array([data.y.item() for data in X_train_out]))
            else:
                skf_inner = StratifiedGroupKFold(n_splits=N_INNER_SPLITS, random_state=1111)
                merged_labels_inner = merge_y_and_others(torch.cat([data.y for data in X_train_out], dim=0),
                                                         torch.cat([data.index for data in X_train_out], dim=0))
                skf_inner_generator = skf_inner.split(np.zeros((len(X_train_out), 1)),
                                                      merged_labels_inner,
                                                      groups=[data.hcp_id.item() for data in X_train_out])
            model_with_sigmoid = True
            metrics = ['auc', 'loss']

            # This for-cycle will only be executed once (for now)
            for inner_train_index, inner_val_index in skf_inner_generator:
                if ANALYSIS_TYPE == AnalysisType.SPATIOTEMOPRAL:
                    if ENCODING_STRATEGY != EncodingStrategy.NONE:
                        if ENCODING_STRATEGY == EncodingStrategy.AE3layers:
                            from encoders import AE  # Necessary to torch.load
                        elif ENCODING_STRATEGY == EncodingStrategy.VAE3layers:
                            from encoders import VAE  # Necessary to torch.load
                        encoding_model = torch.load(create_best_encoder_name(ts_length=TIME_LENGTH,
                                                                             outer_split_num=outer_split_num,
                                                                             encoder_name=ENCODING_STRATEGY.value))
                    else:
                        encoding_model = None
                    model = SpatioTemporalModel(num_time_length=TIME_LENGTH,
                                                dropout_perc=params['dropout'],
                                                pooling=POOLING,
                                                channels_conv=CHANNELS_CONV,
                                                activation=ACTIVATION,
                                                conv_strategy=CONV_STRATEGY,
                                                add_gat=ADD_GAT,
                                                add_gcn=ADD_GCN,
                                                final_sigmoid=model_with_sigmoid,
                                                num_nodes=NUM_NODES,
                                                encoding_model=encoding_model
                                                ).to(device)
                    trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
                    print("Number of trainable params:", trainable_params)
                elif ANALYSIS_TYPE == AnalysisType.FLATTEN_CORRS or ANALYSIS_TYPE == AnalysisType.FLATTEN_CORRS_THRESHOLD:
                    model = XGBClassifier(n_jobs=-1, seed=1111, random_state=1111, **params)

                # Creating the various names for each metric
                model_names = {}
                for m in metrics:
                    model_names[m] = create_name_for_model(TARGET_VAR, model, params, outer_split_num, 0, N_EPOCHS,
                                                           THRESHOLD, BATCH_SIZE, REMOVE_NODES, NUM_NODES, CONN_TYPE,
                                                           NORMALISATION, ANALYSIS_TYPE,
                                                           m)

                X_train_in = X_train_out[torch.tensor(inner_train_index)]
                X_val_in = X_train_out[torch.tensor(inner_val_index)]

                print("Inner Size is:", len(X_train_in), "/", len(X_val_in))
                print("Inner Positive classes:", sum([data.y.item() for data in X_train_in]),
                      "/", sum([data.y.item() for data in X_val_in]))

                if ANALYSIS_TYPE == AnalysisType.FLATTEN_CORRS or ANALYSIS_TYPE == AnalysisType.FLATTEN_CORRS_THRESHOLD:
                    X_train_in_array, y_train_in_array = get_array_data(X_train_in, num_nodes=NUM_NODES)
                    X_val_in_array, y_val_in_array = get_array_data(X_val_in, num_nodes=NUM_NODES)

                    model.fit(X_train_in_array, y_train_in_array)
                    y_pred = model.predict(X_val_in_array)

                    val_metrics = return_metrics(y_val_in_array, y_pred, y_pred)
                    print(val_metrics)
                    if val_metrics['auc'] > best_outer_metric_auc:
                        pickle.dump(model, open(model_names['auc'], "wb"))
                        best_outer_metric_auc = val_metrics['auc']
                        best_model_name_outer_fold_auc = model_names['auc']
                    break

                ###########
                ### DataLoaders
                train_in_loader = DataLoader(X_train_in, batch_size=BATCH_SIZE, shuffle=True)
                val_loader = DataLoader(X_val_in, batch_size=BATCH_SIZE, shuffle=False)

                optimizer = torch.optim.Adam(model.parameters(),
                                             lr=params['lr'],
                                             weight_decay=params['weight_decay'])

                best_metrics_fold = {}
                for m in metrics:
                    if m == 'loss':
                        best_metrics_fold[m] = 1000
                    else:
                        best_metrics_fold[m] = -1000
                # Only for loss
                last_losses_val = deque([1000 for _ in range(EARLY_STOP_STEPS)], maxlen=EARLY_STOP_STEPS)

                for epoch in range(1, N_EPOCHS):
                    if TARGET_VAR == 'gender':
                        val_metrics = classifier_step(outer_split_num,
                                                      0,
                                                      epoch,
                                                      model,
                                                      train_in_loader,
                                                      val_loader)
                        if sum([val_metrics['loss'] > loss for loss in last_losses_val]) == EARLY_STOP_STEPS:
                            print("EARLY STOPPING IT")
                            break
                        last_losses_val.append(val_metrics['loss'])

                        if val_metrics['loss'] < best_metrics_fold['loss']:
                            best_metrics_fold['loss'] = val_metrics['loss']
                            torch.save(model, model_names['loss'])
                            if val_metrics['loss'] < best_outer_metric_loss:
                                best_outer_metric_loss = val_metrics['loss']
                                best_model_name_outer_fold_loss = model_names['loss']

                break  # Just one inner "loop"

        # After all parameters are searched, get best and train on that, evaluating on test set
        if ANALYSIS_TYPE == AnalysisType.SPATIOTEMOPRAL:
            print("Best params if loss: ", best_model_name_outer_fold_loss, "(", best_outer_metric_loss, ")")
            model = torch.load(best_model_name_outer_fold_loss)
            saving_path = best_model_name_outer_fold_loss.replace('logs/', '').replace('.pth', '.npy')
            test_metrics = evaluate_classifier(test_out_loader, save_path_preds=saving_path)

            print('{:1d}-Final: {:.7f}, Auc: {:.4f}, Acc: {:.4f}, Sens: {:.4f}, Speci: {:.4f}'
                  ''.format(outer_split_num, test_metrics['loss'], test_metrics['auc'], test_metrics['acc'],
                            test_metrics['sensitivity'], test_metrics['specificity']))

        elif ANALYSIS_TYPE == AnalysisType.FLATTEN_CORRS or ANALYSIS_TYPE == AnalysisType.FLATTEN_CORRS_THRESHOLD:
                print(f'Best params if auc: {best_model_name_outer_fold_auc} ( {best_outer_metric_auc} )')
                model = pickle.load(open(best_model_name_outer_fold_auc, "rb"))

                X_test_array, y_test_array = get_array_data(X_test_out, num_nodes=NUM_NODES)
                y_pred = model.predict(X_test_array)
                test_metrics = return_metrics(y_test_array, y_pred, y_pred)
                print('{:1d}-Final: Auc: {:.4f}, Acc: {:.4f}, Sens: {:.4f}, Speci: {:.4f}'
                      ''.format(outer_split_num, test_metrics['auc'], test_metrics['acc'],
                                test_metrics['sensitivity'], test_metrics['specificity']))

                save_path_preds = best_model_name_outer_fold_auc.replace('logs/', '').replace('.pkl', '.npy')

                np.save('results/labels_' + save_path_preds, y_test_array)
                np.save('results/predictions_' + save_path_preds, y_pred)

    total_seconds = time.time() - start_time
    total_time = str(datetime.timedelta(seconds=total_seconds))
    print(f'--- {total_seconds} seconds to execute this script ({total_time})---')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument("--device", default="cuda")

    parser.add_argument("--fold_num", type=int)
    parser.add_argument("--target_var")
    parser.add_argument("--activation", default='relu')
    parser.add_argument("--threshold", default=5, type=int)
    parser.add_argument("--num_nodes", default=50, type=int)
    parser.add_argument("--num_epochs", default=100, type=int)
    parser.add_argument("--batch_size", default=150, type=int)
    parser.add_argument("--add_gcn", type=bool, default=False)  # to make true just include flag with 1
    parser.add_argument("--add_gat", type=bool, default=False)  # to make true just include flag with 1
    parser.add_argument("--remove_disconnected_nodes", type=bool,
                        default=False)  # to make true just include flag with 1
    parser.add_argument("--conn_type", default="struct")
    parser.add_argument("--conv_strategy", default="entire")
    parser.add_argument("--pooling",
                        default="mean")
    parser.add_argument("--channels_conv", type=int)
    parser.add_argument("--normalisation", default='roi_norm')
    parser.add_argument("--analysis_type", default='spatiotemporal')
    parser.add_argument("--time_length", type=int)
    parser.add_argument("--encoding_strategy", default='none')
    parser.add_argument("--early_stop_steps", default=30, type=int)

    TODO: check "gcn_layers"" option
    TODO: check how to give name to a specific sweep (from sweep keys?)

    args = parser.parse_args()

    sweep_config = SWEEP_GENERAL
    sweep_config['name'] = NNNN
    # also, put the n_epoch in the config part.
    sweep_id = wandb.sweep(sweep_config, entity='tjiagom', project="pytorch-intro")

    wandb.agent(sweep_id, function=main_loop)