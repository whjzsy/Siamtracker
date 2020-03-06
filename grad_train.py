import os
import time
import logging
import json
import argparse
import torch
import torch.nn as nn
from torch.nn.utils import clip_grad_norm_
from torch.utils.data import DataLoader
from torch.optim import Adam
from dataset.dataset import Dataset
from configs.config import cfg
from utils.model_load import load_pretrain
from models import get_model
from utils.log_helper import init_log, add_file_handler, print_speed
from utils.misc import commit, describe
from utils.average_meter import AverageMeter

logger = logging.getLogger("global")
parser = argparse.ArgumentParser()
parser.add_argument("--cfg", default="", type=str, help="which config file to use")
args = parser.parse_args()


def build_dataloader():
    logger.info("building datalaoder!")
    grad_dataset = Dataset()
    graph_dataloader = DataLoader(grad_dataset, batch_size=cfg.GRAD.BATCH_SIZE, shuffle=False)
    return graph_dataloader


def build_optimizer(model, current_epoch=0):
    logger.info("build optimizer!")
    for param in model.backbone.parameters():
        param.requires_grad = False
    for m in model.backbone.modules():
        if isinstance(m, nn.BatchNorm2d):
            m.eval()
    trainable_param = []
    trainable_param += [
        {"params": model.grad_layer.parameters(), "lr": cfg.GRAD.LR},  # TODO: may be can be optimized
    ]
    optimizer = Adam(trainable_param, weight_decay=cfg.GRAD.WEIGHT_DECAY)
    return optimizer


def train(dataloader, optimizer, model):
    iter = 0
    begin_time = 0.0
    average_meter = AverageMeter()
    num_per_epoch = len(dataloader.dataset) // (cfg.GRAD.BATCH_SIZE)
    for epoch in range(cfg.GRAD.EPOCHS):
        dataloader.dataset.shuffle()
        if epoch==cfg.BACKBONE.TRAIN_EPOCH:
            logger.info('begin to train backbone!')
            optimizer = build_optimizer(model, epoch)
            logger.info("model\n{}".format(describe(model)))
        begin_time = time.time()
        for data in dataloader:
            examplar_imgs = data['examplars'].cuda()
            search_img = data['search'].cuda()
            gt_cls = data['gt_cls'].cuda()
            gt_delta = data['gt_delta'].cuda()
            delta_weight = data['gt_delta_weight'].cuda()
            data_time = time.time() - begin_time

            losses = model.forward(examplar_imgs, search_img, gt_cls, gt_delta, delta_weight)
            cls_loss = losses['cls_loss']
            loc_loss = losses['loc_loss']
            loss = losses['total_loss']
            optimizer.zero_grad()
            loss.backward()
            clip_grad_norm_(model.parameters(), cfg.TRAIN.GRAD_CLIP)
            optimizer.step()
            batch_time = time.time() - begin_time
            batch_info = {}
            batch_info['data_time'] = data_time
            batch_info['batch_time'] = batch_time
            average_meter.update(**batch_info)
            if iter % cfg.TRAIN.PRINT_EVERY == 0:
                logger.info('epoch: {}, iter: {}, cls_loss: {}, loc_loss: {}, loss: {}'
                            .format(epoch + 1, iter, cls_loss.item(), loc_loss.item(), loss.item()))
                print_speed(iter + 1,
                            average_meter.batch_time.avg,
                            cfg.GRAD.EPOCHS * num_per_epoch)
            begin_time = time.time()
            iter += 1
        # save train_state
        if not os.path.exists(cfg.GRAD.SNAPSHOT_DIR):
            os.makedirs(cfg.GRAD.SNAPSHOT_DIR)
        # put the update to the rpn state
        state = {
            "model": model.state_dict(),
            "optimizer": optimizer.state_dict(),
            "epoch": epoch,
        }
        save_path = "{}/checkpoint_e{}.pth".format(cfg.GRAD.SNAPSHOT_DIR, epoch)
        logger.info("save state to {}".format(save_path))
        torch.save(state, save_path)


def main():
    cfg.merge_from_file(args.cfg)
    if not os.path.exists(cfg.GRAD.LOG_DIR):
        os.makedirs(cfg.GRAD.LOG_DIR)
    init_log("global", logging.INFO)
    if cfg.GRAD.LOG_DIR:
        add_file_handler(
            "global", os.path.join(cfg.GRAD.LOG_DIR, "logs.txt"), logging.INFO
        )
    logger.info("Version Information: \n{}\n".format(commit()))
    logger.info("config \n{}".format(json.dumps(cfg, indent=4)))
    model = get_model('GradSiamModel').cuda()
    model = load_pretrain(model, cfg.GRAD.PRETRAIN_PATH)
    # parametes want to optim
    optimizer = build_optimizer(model)
    dataloader = build_dataloader()
    model.freeze_model()
    train(dataloader, optimizer, model)


if __name__ == "__main__":
    main()


