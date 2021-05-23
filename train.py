import argparse

from torch.nn.modules import dropout
from utils.utils import normtensor, tensor2np, wandb_mask

from model.metric import cal_iou, get_iou_score
import os
from model.loss import DiceLoss
from model.config import BATCH_SIZE, DATA_PATH, DROP_RATE, EPOCHS, INPUT_SIZE, LEARNING_RATE, N_CLASSES, RUN_NAME, SAVE_PATH, START_FRAME

import torch
import wandb
import time
import numpy as np
from tqdm import tqdm
from torchsummary import summary
from torch import optim
from torch import nn
from model.model import UNet, UNet_ResNet
from utils.dataset import TGSDataset, get_dataloader
from utils.utils import show_dataset, show_image_mask

import matplotlib.pyplot as plt

def parse_args():
    """parse command line arguments"""
    parser = argparse.ArgumentParser(description='Train image segmentation')
    parser.add_argument('--run', type=str, default='demo', help="run name")
    parser.add_argument('--model', type=str, default='Unet', help="initial weights path")
    parser.add_argument('--dropout', type=int, default=DROP_RATE, help="declear dropout rate")
    parser.add_argument('--epoch', type=int, default=EPOCHS, help="number of epoch")
    parser.add_argument('--startfm', type=int, default=START_FRAME, help="architecture start frame")
    parser.add_argument('--batchsize', type=int, default=BATCH_SIZE, help="total batch size for all GPUs (default:")
    parser.add_argument('--lr', type=float, default=LEARNING_RATE, help="learning rate (default: 0.0001)")
    parser.add_argument('--size', type=int, default=INPUT_SIZE, help="learning rate (default: 0.0001)")

    args = parser.parse_args()
    return args

def train(model, device, trainloader, optimizer, loss_function):
    model.train()
    running_loss = 0
    mask_list, iou = [], []
    for i, (input, mask) in enumerate(trainloader):
        # load data into cuda
        input, mask = input.to(device), mask.to(device)

        # forward
        predict = model(input)
        loss = loss_function(predict, mask)

        # metric
        iou.append(get_iou_score(predict, mask).mean())
        running_loss += (loss.item())
        
        # zero the gradient + backprpagation + step
        optimizer.zero_grad()

        loss.backward()
        optimizer.step()

        # log the first image of the batch
        if ((i + 1) % 10) == 0:
            pred = normtensor(predict[0])
            img, pred, mak = tensor2np(input[0]), tensor2np(pred), tensor2np(mask[0])
            mask_list.append(wandb_mask(img, pred, mak))
            
    mean_iou = np.mean(iou)
    total_loss = running_loss/len(trainloader)
    wandb.log({'Train loss': total_loss, 'Train IoU': mean_iou, 'Train prediction': mask_list})

    return total_loss, mean_iou
    
def test(model, device, testloader, loss_function, best_iou):
    model.eval()
    running_loss = 0
    mask_list, iou  = [], []
    with torch.no_grad():
        for i, (input, mask) in enumerate(testloader):
            input, mask = input.to(device), mask.to(device)

            predict = model(input)
            loss = loss_function(predict, mask)

            running_loss += loss.item()
            iou.append(get_iou_score(predict, mask).mean())

            # log the first image of the batch
            if ((i + 1) % 1) == 0:
                pred = normtensor(predict[0])
                img, pred, mak = tensor2np(input[0]), tensor2np(pred), tensor2np(mask[0])
                mask_list.append(wandb_mask(img, pred, mak))

    test_loss = running_loss/len(testloader)
    mean_iou = np.mean(iou)
    wandb.log({'Valid loss': test_loss, 'Valid IoU': mean_iou, 'Prediction': mask_list})
    
    if mean_iou>best_iou:
    # export to onnx + pt
        try:
            torch.onnx.export(model, input, SAVE_PATH+RUN_NAME+'.onnx')
            torch.save(model.state_dict(), SAVE_PATH+RUN_NAME+'.pth')
        except:
            print('Can export weights')

    return test_loss, mean_iou

if __name__ == '__main__':
    args = parse_args()

    # init wandb
    config = dict(
        model       = args.model,
        dropout     = args.dropout,
        lr          = args.lr,
        batchsize   = args.batchsize,
        epoch       = args.epoch,
        model_sf    = args.startfm,
        size        = args.size
    )
    
    RUN_NAME = args.run
    INPUT_SIZE = args.size

    run = wandb.init(project="TGS-Salt-identification", tags=['Unet'], config=config)
    artifact = wandb.Artifact('tgs-salt', type='dataset')

    # train on device
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    print("Current device", device)

    try:
        artifact.add_dir(DATA_PATH)
        run.log_artifact(artifact)
    except:
        artifact     = run.use_artifact('tgs-salt:latest')
        artifact_dir = artifact.download(DATA_PATH)


    # load dataset
    dataset = TGSDataset(DATA_PATH)
    trainloader, validloader = get_dataloader(dataset=dataset, batch_size=args.batchsize)

    # get model and define loss func, optimizer
    n_classes = N_CLASSES
    epochs = args.epoch

    if args.model == 'Unet':
        model = UNet(start_fm=args.model_sf).to(device)
    else:
        model = UNet_ResNet(dropout=args.dropout, start_fm=args.model_sf).to(device)
    
    # summary model
    summary = summary(model, input_size=(1, args.size, args.size))

    criterion = nn.BCEWithLogitsLoss()

    # loss_func   = Weighted_Cross_Entropy_Loss()
    optimizer   = optim.Adam(model.parameters(), lr=args.lr)

    # wandb watch
    run.watch(models=model, criterion=criterion, log='all', log_freq=10)

    # training
    best_iou = -1

    for epoch in range(epochs):
        t0 = time.time()
        train_loss, train_iou = train(model, device, trainloader, optimizer, criterion)
        t1 = time.time()
        print(f'Epoch: {epoch} | Train loss: {train_loss:.3f} | Train IoU: {train_iou:.3f} | Time: {(t1-t0):.1f}s')
        test_loss, test_iou = test(model, device, validloader, criterion, best_iou)
        print(f'Epoch: {epoch} | Valid loss: {test_loss:.3f} | Valid IoU: {test_iou:.3f} | Time: {(t1-t0):.1f}s')
        
        # Wandb summary
        if best_iou < train_iou:
            best_iou = train_iou
            wandb.run.summary["best_accuracy"] = best_iou

    # evaluate