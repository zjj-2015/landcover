import argparse
import numpy as np
from pprint import pprint
from attr import attrs, attrib
from einops import rearrange
import pdb
from datetime import datetime, timedelta
from pathlib import Path
from itertools import product
import csv
import math
import copy
import json
import os
import random

import torch
import torch.nn as nn
from torch.utils import data
from torch.optim import lr_scheduler

from training.pytorch.models.unet import Unet
from training.pytorch.models.fusionnet import Fusionnet
from training.pytorch.utils.save_visualize import save_visualize
from training.pytorch.utils.eval_segm import mean_IoU, pixel_accuracy
from training.pytorch.utils.experiments_utils import improve_reproducibility
from training.pytorch.utils.filesystem import ensure_dir
from training.pytorch.losses import (multiclass_ce, multiclass_dice_loss, multiclass_jaccard_loss, multiclass_tversky_loss, multiclass_ce_points)
from training.pytorch.data_loader import DataGenerator
from training.pytorch.utils.data.tile_to_npy import sample


parser = argparse.ArgumentParser()

parser.add_argument('--config_file', type=str, default="/mnt/blobfuse/train-output/conditioning/models/backup_unet_gn_isotropic_nn9/training/params.json", help="json file containing the configuration")

parser.add_argument('--model_file', type=str,
                    help="Checkpoint saved model",
                    default="/mnt/blobfuse/train-output/conditioning/models/backup_unet_gn_isotropic_nn9/training/checkpoint_best.pth.tar")

#parser.add_argument('--data_path', type=str, help="Path to data", default="/mnt/blobfuse/cnn-minibatches/summer_2019/active_learning_splits/")
# parser.add_argument('--data_sub_dirs', type=str, nargs='+', help="Sub-directories of `data_path` to get data from", default=['val1',]) # 'test1', 'test2', 'test3', 'test4'])

parser.add_argument('--run_validation', action="store_true", help="Whether to run validation")
#parser.add_argument('--validation_patches_fn', type=str, help="Filename with list of validation patch files", default='training/data/finetuning/val2_test_patches_500.txt')
parser.add_argument('--validation_patches_fn', type=str, help="Filename with list of training patch files", default="training/data/finetuning/val2_train_patches_5.txt")
parser.add_argument('--training_patches_fn', type=str, help="Filename with list of training patch files", default="training/data/finetuning/val2_train_patches_5.txt")
parser.add_argument('--training_tile_fn', type=str, help="Filename with list of training tile files", default="training/data/finetuning/test1_train_tiles.txt")

parser.add_argument('--log_fn', type=str, help="Where to store training results", default="/mnt/blobfuse/train-output/conditioning/models/backup_unet_gn_isotropic_nn9/finetuning/val/val2/10_patches/finetune_results.csv")

parser.add_argument('--model_output_directory', help='Where to store fine-tuned model', default='/mnt/blobfuse/train-output/conditioning/models/backup_unet_gn_isotropic_nn9/finetuning/val/val2_fix/')



args = parser.parse_args()

class GroupParams(nn.Module):

    def __init__(self, model):
        super(GroupParams, self).__init__()
        self.gammas = nn.Parameter(torch.ones((1, 32, 1, 1)))
        self.betas = nn.Parameter(torch.zeros((1, 32, 1, 1)))
        self.model = model

    def forward(self, x):
        x, conv1_out, conv1_dim = self.model.down_1(x)

        x, conv2_out, conv2_dim = self.model.down_2(x)

        x, conv3_out, conv3_dim = self.model.down_3(x)
        x, conv4_out, conv4_dim = self.model.down_4(x)

        # Bottleneck
        x = self.model.conv5_block(x)

        # up layers
        x = self.model.up_1(x, conv4_out, conv4_dim)
        x = self.model.up_2(x, conv3_out, conv3_dim)
        x = self.model.up_3(x, conv2_out, conv2_dim)
        x = self.model.up_4(x, conv1_out, conv1_dim)
        x = x * self.gammas + self.betas

        return self.model.conv_final(x)


@attrs
class FineTuneResult(object):
    best_mean_IoU = attrib(type=float)
    train_duration = attrib(type=timedelta)
    
    
def finetune_group_params(path_2_saved_model, loss, gen_loaders, params, hyper_parameters, log_writer, n_epochs=25):
    learning_rate = hyper_parameters['learning_rate']
    optimizer_method = hyper_parameters['optimizer_method']
    lr_schedule_step_size = hyper_parameters['lr_schedule_step_size']
    if 'epochs' in hyper_parameters:
        n_epochs = hyper_parameters['epochs']
    
    opts = params["model_opts"]
    unet = Unet(opts)
    checkpoint = torch.load(path_2_saved_model)
    unet.load_state_dict(checkpoint['model'])
    unet.eval()
    for param in unet.parameters():
        param.requires_grad = False

    # Parameters of newly constructed modules have requires_grad=True by default
    model_2_finetune = GroupParams(unet)
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    model_2_finetune = model_2_finetune.to(device)
    loss = loss().to(device)

    optimizer = torch.optim.SGD(model_2_finetune.parameters(), lr=learning_rate, momentum=0.9)
    if optimizer_method == torch.optim.Adam:
        optimizer = torch.optim.Adam(model_2_finetune.parameters(), lr=learning_rate, eps=1e-5)
    
    # Decay LR by a factor of 0.1 every 7 epochs
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=lr_schedule_step_size, gamma=0.1)

    model_2_finetune = active_learning(model_2_finetune, loss, optimizer,
                                   exp_lr_scheduler, gen_loaders, hyper_parameters, log_writer, num_epochs=n_epochs)
    return model_2_finetune

def finetune_last_k_layers(path_2_saved_model, loss, gen_loaders, params, hyper_parameters, log_writer, n_epochs=25):
    learning_rate = hyper_parameters['learning_rate']
    optimizer_method = hyper_parameters['optimizer_method']
    lr_schedule_step_size = hyper_parameters['lr_schedule_step_size']
    last_k_layers = hyper_parameters['last_k_layers']
    if 'epochs' in hyper_parameters:
        n_epochs = hyper_parameters['epochs']
    
    opts = params["model_opts"]
    unet = Unet(opts)
    checkpoint = torch.load(path_2_saved_model)
    unet.load_state_dict(checkpoint['model'])
    unet.eval()

    # pdb.set_trace()
    
    for layer in list(unet.children())[:-last_k_layers]:
        for param in layer.parameters():
            param.requires_grad = False
    
    # Parameters of newly constructed modules have requires_grad=True by default
    model_2_finetune = unet
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    model_2_finetune = model_2_finetune.to(device)
    loss = loss().to(device)

    optimizer = torch.optim.SGD(model_2_finetune.parameters(), lr=learning_rate, momentum=0.9)
    if optimizer_method == torch.optim.Adam:
        optimizer = torch.optim.Adam(model_2_finetune.parameters(), lr=learning_rate, eps=1e-5)
        
    # Decay LR by a factor of 0.1 every 7 epochs
    exp_lr_scheduler = lr_scheduler.StepLR(optimizer, step_size=lr_schedule_step_size, gamma=0.1)

    model_2_finetune = active_learning(model_2_finetune, loss, optimizer,
                                   exp_lr_scheduler, gen_loaders, hyper_parameters, log_writer, num_epochs=n_epochs)
    return model_2_finetune


def active_learning_step_size(num_points):
    if num_points < 40:
        return 10
    if num_points < 400:
        return 100
    if num_points < 4000:
        return 1000


def evaluate_model(model, train_tile):
    pass


def prediction_entropy(predictions):
    # predictions: (channels, height, width)
    return (predictions * predictions.log()).sum(axis=0)


def pixels_to_patches(train_tile, points):
    # return one 240 x 240 patch per point
    pass



def new_train_patches_entropy(model, train_tile, num_new_patches):
    predictions = evaluate_model(model, train_tile)
    # (channels, height, width)
    entropy = prediction_entropy(predictions)
    # (height, width)
    rows, columns = entropy.shape
    possible_indices = [(row, column) for row in range(rows) for column in range(columns)]
    highest_entropy_points = heapq.nlargest(num_train_points,
                                            possible_indices,
                                            key=lambda index: entropy[index])
    new_train_patches = pixels_to_patches(train_tile, highest_entropy_points)


# If needed: implement the below function for generating random patches
#def new_train_patches_random(model, train_tile, num_new_patches):
    # sample(train_tile)
    # new_train_patches = pixels_to_patches(train_tile, highest_entropy_points)



    
def active_learning(model, loss_criterion, optimizer, scheduler, dataloaders, hyper_parameters, log_writer, num_epochs=20, superres=False, masking=True, step_size_function=active_learning_step_size, new_train_patches_function=new_train_patches_entropy, num_total_points=4000):
    train_tile = None  # TODO: get this from somewhere
    old_model = copy.deepcopy(model)
    training_patches = []

    while len(training_patches) < num_total_points:
        num_new_patches = step_size_function(len(training_patches))
        training_patches += new_train_patches_function(model, train_tile, num_new_patches)
        model = copy.deepcopy(old_model)
        new_model, fine_tune_result = train_model(model, loss_criterion, optimizer, scheduler, dataloaders, hyper_parameters, log_writer, num_epochs=20, superres=False, masking=True)

        
    

def train_model(model, criterion, optimizer, scheduler, dataloaders, hyper_parameters, log_writer, num_epochs=20, superres=False, masking=True):
    global results_writer, results_file
    
    # mask_id indices (points per patch): [1, 2, 3, 4, 5, 10, 15, 20, 40, 60, 80, 100]
    mask_id = hyper_parameters['mask_id']
    
    since = datetime.now()

    best_model_wts = copy.deepcopy(model.state_dict())
    best_mean_IoU = 0.0
    best_epoch = -1
    duration_til_best_epoch = since - since
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

    # Each epoch can have a training and validation phase
    phases = data_loaders.keys()
    for phase in phases:
        if phase not in ['train', 'val']:
            print('Warning: epoch phase "%s" not valid. Valid options: ["train", "val"]. Data provided in this phase may be ignored.' % phase)
    
    for epoch in range(-1, num_epochs):
        #print('Epoch {}/{}'.format(epoch, num_epochs - 1))
        #print('-' * 10)
        
        statistics = {
            'mean_IoU': -1,
            'loss': -1,
            'accuracy': -1
        }

        epoch_statistics = {
            phase: copy.deepcopy(statistics)
            for phase in phases
        }
        # print(epoch_statistics)

        hyper_parameters['epoch'] = epoch
        
        for phase in phases:
            if phase == 'train':
                scheduler.step()
                model.train()  # Set model to training mode
            else:  # phase == 'val'
                if 'val' in dataloaders:
                    model.eval()   # Set model to evaluate mode
                else:
                    continue
                
            epoch_statistics[phase]['loss'] = 0.0
            epoch_statistics[phase]['mean_IoU'] = 0.0
            epoch_statistics[phase]['accuracy'] = 0.0
            
            running_loss = 0.0
            meanIoU = 0.0
            accuracy = 0.0
            
            n_iter = 0

            # Iterate over data.
            for entry in dataloaders[phase]:
                if superres:
                    if masking:
                        inputs, labels, nlcd, masks = entry
                    else:
                        inputs, labels, nlcd = entry
                    # TODO: use nlcd for superres training, below
                else:
                    if masking:
                        inputs, labels, masks = entry
                    else:
                        inputs, labels = entry

                inputs = inputs[:, :, 2:240 - 2, 2:240 - 2]
                labels = labels[:, :, 94:240 - 94, 94:240 - 94]
                
                inputs = inputs.to(device)
                labels = labels.to(device)

                if masking and phase == 'train':
                    masks = masks.float()
                    masks = masks.to(device)
                    masks = rearrange(masks, 'batch unknown masks height width -> batch (unknown masks) height width')
                    mask = masks[:, mask_id : mask_id + 1, 94:240 - 94, 94:240 - 94].to(device)
                    labels = labels * mask

                # zero the parameter gradients
                optimizer.zero_grad()

                # forward
                # track history if only in train
                with torch.set_grad_enabled(phase == 'train' and epoch > -1):
                    outputs = model.forward(inputs)
                    ground_truth = torch.squeeze(labels,1).long()
                    # print(outputs.shape)
                    # print(ground_truth.shape)
                    path = str(Path(args.model_output_directory) / ("epoch_" + str(epoch) + "_" + phase))
                    ensure_dir(path)
                    # print('Save to path: %s' % path)
                    # save_visualize(inputs, outputs, ground_truth, path)

                    loss = criterion(ground_truth, outputs)

                    # backward + optimize only if in training phase
                    if phase == 'train' and epoch > -1:
                        loss.backward()
                        optimizer.step()

                # Store ground truth
                y_hr = np.squeeze(labels.cpu().numpy(), axis=1)
                # TODO: I think we need the below... causes error though:
                #if phase == 'train':
                    #y_hr = y_hr * mask.cpu().detach().numpy()
                
                # Store current outputs
                batch_size, _, _ = y_hr.shape
                # TODO: do we need this check below?
                if phase == 'train':
                    y_hat = outputs.cpu().detach().numpy() * mask.cpu().detach().numpy()
                else:
                    y_hat = outputs.cpu().numpy()
                y_hat = np.argmax(y_hat, axis=1)

                        
                # statistics
                n_iter += 1

                # 1) Loss
                epoch_statistics[phase]['loss'] += loss.item()
                
                # 2) mean_IoU
                batch_meanIoU = 0
                for j in range(batch_size):
                    batch_meanIoU += mean_IoU(y_hat[j], y_hr[j], ignored_classes={0})
                batch_meanIoU /= batch_size
                epoch_statistics[phase]['mean_IoU'] += batch_meanIoU
                
                # 3) accuracy
                batch_accuracy = 0
                for j in range(batch_size):
                    batch_accuracy += pixel_accuracy(y_hat[j], y_hr[j], ignored_classes={0})
                batch_accuracy /= batch_size
                epoch_statistics[phase]['accuracy'] += batch_accuracy
                
            # Normalize statistics per training iteration in epoch
            for key in epoch_statistics[phase]:
                epoch_statistics[phase][key] /= n_iter  # divide by how many batches were processed in this epoch
            # print('number of batches in epoch', len(dataloaders[phase]))
            # print('n_iter', n_iter)
                
        result_row = {
            'run_id': hyper_parameters['run_id'],
            'hyper_parameters': hyper_parameters,
            'epoch': epoch,
            'train_loss': epoch_statistics['train']['loss'],
            'train_accuracy': epoch_statistics['train']['accuracy'],
            'train_mean_IoU': epoch_statistics['train']['mean_IoU'],
            'val_loss': epoch_statistics['val']['loss'],
            'val_accuracy': epoch_statistics['val']['accuracy'],
            'val_mean_IoU': epoch_statistics['val']['mean_IoU'],
            'total_time': datetime.now() - since
        }
        # pprint(result_row)
        results_writer.writerow(result_row)
        results_file.flush()

        model_file_name_suffix = hyper_parameters_str(hyper_parameters)
        
        finetuned_fn = str(Path(args.model_output_directory) / ("finetuned_unet_gn.pth_%s.tar" % model_file_name_suffix))
        torch.save(model.state_dict(), finetuned_fn)
        
            # deep copy the model
            #if phase == 'val' and epoch_mean_IoU > best_mean_IoU:
            #    best_mean_IoU = epoch_mean_IoU
            #    best_model_wts = copy.deepcopy(model.state_dict())
            #    best_epoch = epoch
            #    duration_til_best_epoch = datetime.now() - since
        # print()

    duration = datetime.now() - since
    seconds_elapsed = duration.total_seconds()
    
    ## print('Training complete in {:.0f}m {:.0f}s'.format(
    #    seconds_elapsed // 60, seconds_elapsed % 60))
    ## print('Best val IoU: {:4f}'.format(best_mean_IoU))

    # load best model weights
    # model.load_state_dict(best_model_wts)
    return model, FineTuneResult(best_mean_IoU=best_mean_IoU, train_duration=duration)


def hyper_parameters_str(hyper_parameters):
    # hyper_parameters_str = sorted(hyper_parameters.items())
    hyper_parameters_str = "%s_lr_%f_epoch_%d" % (
        hyper_parameters['method_name'],
        hyper_parameters['learning_rate'],
        hyper_parameters['epoch'],
    )
    if 'last_k_layers' in hyper_parameters:
        hyper_parameters_str += ("_last_k_" + str(hyper_parameters['last_k_layers']))
    return hyper_parameters_str

def main(finetune_methods, predictions_path, validation_patches_fn=None):
    global results_writer, results_file
    
    os.makedirs(str(Path(args.log_fn).parent), exist_ok=True)
    results_file = open(args.log_fn, 'w+')
    results_writer = csv.DictWriter(results_file, ['run_id', 'hyper_parameters', 'epoch', 'train_loss', 'train_accuracy', 'train_mean_IoU', 'val_loss', 'val_accuracy', 'val_mean_IoU', 'total_time'])
    results_writer.writeheader()
    
    params = json.load(open(args.config_file, "r"))
    
    f = open(args.training_patches_fn, "r")
    training_patches = f.read().strip().split("\n")
    f.close()

    validation_patches = None
    if args.validation_patches_fn:
        f = open(args.validation_patches_fn, "r")
        validation_patches = f.read().strip().split("\n")
        f.close()

    # f = open(training_points_sample_fn, "r")
    # training_points = [ for line in f.read().stip().split("\n")]
    
    batch_size = params["loader_opts"]["batch_size"]
    patch_size = params["patch_size"]
    num_channels = params["loader_opts"]["num_channels"]
    params_train = {'batch_size': params["loader_opts"]["batch_size"],
                    'shuffle': params["loader_opts"]["shuffle"],
                    'num_workers': params["loader_opts"]["num_workers"]}
        
    training_set = DataGenerator(
        training_patches, batch_size, patch_size, num_channels, superres=params["train_opts"]["superres"], masking=True
    )

    validation_set = None
    if validation_patches:
        validation_set = DataGenerator(
            validation_patches, batch_size, patch_size, num_channels, superres=params["train_opts"]["superres"], masking=True
        )

    model_opts = params["model_opts"]
    loss = multiclass_ce_points
    path = args.model_file

    dataloaders = {'train': data.DataLoader(training_set, **params_train)}
    if validation_set:
        dataloaders['val'] = data.DataLoader(validation_set, **params_train)

    results = {}
    for run_id, (finetune_method_name, finetune_function, hyper_params) in enumerate(finetune_methods):
        hyper_params['run_id'] = run_id
        
        # print('Fine-tune hyper-params: %s' % str(hyper_params))
        improve_reproducibility()
        model, result = finetune_function(path, loss, dataloaders, params, hyper_params, results_writer, n_epochs=hyper_params['n_epochs']) #, predictions_path=str(predictions_path / str(hyper_params)))
        results[finetune_method_name] = result
        
        savedir = args.model_output_directory
        if not os.path.exists(savedir):
            os.makedirs(savedir)

        # TODO: figure out error, then put this back:
#        if model_opts["model"] == "unet":
#            finetuned_fn = str(Path(savedir) / ("finetuned_unet_gn.pth_%s.tar" % str(hyper_params)))
#            torch.save(model.state_dict(), finetuned_fn)

    # pprint(results)
    results_file.close()


def hyper_parameters_search(hyper_parameters):
    experiment_configs = []

    num_learning_rates = 6
    num_last_k_layers = 6

    # Random hyper-parameter sweep
    for method_name in ['last_k_layers', 'group_params']:
        for mask_id in [0, 4, 7, 11]:  # 1, 5, 20, or 100 points per patch
            if method_name == 'last_k_layers':
                num_iters = num_learning_rates * num_last_k_layers
            else:
                num_iters = num_learning_rates
            
            for i in range(num_iters):
                hyper_parameters['method_name'] = method_name
                hyper_parameters['mask_id'] = mask_id
                hyper_parameters['learning_rate'] = math.pow(10, random.gauss(-2.55, 0.85))   # random sample centered between 10^-3.4 and 10^-1.7 (0.0004 to 0.02), sampled on log-Gaussian scale
            
                if method_name == 'last_k_layers':
                    fine_tune_function = finetune_last_k_layers
                    hyper_parameters['last_k_layers'] = random.randint(1, 10) # model has 10 layers; in the extreme case, we re-train the whole network...
                else:
                    fine_tune_function = finetune_group_params

                experiment_configs += [(method_name, fine_tune_function, hyper_parameters)]
                hyper_parameters = copy.deepcopy(hyper_parameters)
    return experiment_configs


def hyper_parameters_fixed(hyper_parameters):
    experiment_configs = []

    # Add last-k-layers hypers
    for last_k_layers, learning_rate, last_epoch in [(1, 0.015, 1), (2, 0.0006, 8), (3, 0.0045, 0)]:
        new_hyper_parameters = copy.deepcopy(hyper_parameters)
        new_hyper_parameters['method_name'] = 'last_k_layers'
        new_hyper_parameters['last_k_layers'] = last_k_layers
        new_hyper_parameters['learning_rate'] = learning_rate
        new_hyper_parameters['epochs'] = last_epoch + 1
        experiment_configs += [(new_hyper_parameters['method_name'], finetune_last_k_layers, new_hyper_parameters)]

    # Add group-params method
    new_hyper_parameters = copy.deepcopy(hyper_parameters)
    new_hyper_parameters['method_name'] = 'group_params'
    new_hyper_parameters['learning_rate'] = 0.0025
    new_hyper_parameters['epochs'] = 10 + 1
    experiment_configs += [(new_hyper_parameters['method_name'], finetune_group_params, new_hyper_parameters)]

    return experiment_configs

    
def product_dict(**kwargs):
    keys = kwargs.keys()
    vals = kwargs.values()
    for instance in product(*vals):
        yield dict(zip(keys, instance))

        
if __name__ == "__main__":
    # mask_id indices (points per patch): [1, 2, 3, 4, 5, 10, 15, 20, 40, 60, 80, 100]

    hyper_parameters_init = {
        'method_name': 'last_k_layers',
        'optimizer_method': torch.optim.Adam, #, torch.optim.SGD],
        'learning_rate': 0.004, # [0.001, 0.002, 0.003, 0.004, 0.01, 0.03],
        'lr_schedule_step_size': 1000,  # [5],
        'mask_id': 5, #  [0, 4, 7, 11] # range(12) # [4], # mask-id 5 --> 10 px / patch
        'n_epochs': 30,
    }

    if args.run_validation:
        experiment_configs = hyper_parameters_search(hyper_parameters_init)
    else:
        experiment_configs = hyper_parameters_fixed(hyper_parameters_init)
    
    
    predictions_path = Path(args.model_output_directory) / "predictions"
    # pdb.set_trace()
    main(experiment_configs, predictions_path)

