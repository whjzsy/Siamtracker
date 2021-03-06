import gc
import json
import math
import os
import logging
import time
import random
import argparse
import numpy as np
import torch
from torch import optim
from torch.nn.utils import clip_grad_norm_
from torch.utils.data import DataLoader
from tensorboardX import SummaryWriter
# from memory_profiler import profile
# import psutil
from configs.config import cfg
from dataset.dataset import TrainDataset
from models.pruning_siam_model import PruningSiamModel
from utils.log_helper import init_log, add_file_handler, print_speed
from utils.lr_scheduler import build_lr_scheduler
from utils.misc import commit, describe
from utils.model_load import load_pretrain, restore_from
from utils.average_meter import AverageMeter

logger = logging.getLogger('global')

parser = argparse.ArgumentParser()
parser.add_argument('--cfg', default='', type=str, help='which config file to use')
args = parser.parse_args()

os.environ["CUDA_VISIBLE_DEVICES"] = "1"


def seed_torch(seed=0):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


def log_grads(model, tb_writer, tb_index):
    def weights_grads(model):
        grad = {}
        weights = {}
        for name, param in model.named_parameters():
            if param.grad is not None:
                grad[name] = param.grad
                weights[name] = param.data
        return grad, weights

    grad, weights = weights_grads(model)
    feature_norm, rpn_norm = 0, 0
    for k, g in grad.items():
        _norm = g.data.norm(2)
        weight = weights[k]
        w_norm = weight.norm(2)
        if 'feature' in k:
            feature_norm += _norm ** 2
        else:
            rpn_norm += _norm ** 2

        tb_writer.add_scalar('grad_all/' + k.replace('.', '/'),
                             _norm, tb_index)
        tb_writer.add_scalar('weight_all/' + k.replace('.', '/'),
                             w_norm, tb_index)
        tb_writer.add_scalar('w-g/' + k.replace('.', '/'),
                             w_norm / (1e-20 + _norm), tb_index)
    tot_norm = feature_norm + rpn_norm
    tot_norm = tot_norm ** 0.5
    feature_norm = feature_norm ** 0.5
    rpn_norm = rpn_norm ** 0.5

    tb_writer.add_scalar('grad/tot', tot_norm, tb_index)
    tb_writer.add_scalar('grad/feature', feature_norm, tb_index)
    tb_writer.add_scalar('grad/head', rpn_norm, tb_index)


def build_optimizer_lr(model, current_epoch=0):
    trainable_param = []
    trainable_param += [{
        'params': filter(lambda x: x.requires_grad, model.backbone.parameters()),
        'lr': cfg.PRUNING.BASE_LR
    }]

    if cfg.ADJUST.USE:
        trainable_param += [{
            'params': model.neck.parameters(),
            'lr': cfg.PRUNING.BASE_LR
        }]
    trainable_param += [{
        'params': model.rpn.parameters(),
        'lr': cfg.PRUNING.BASE_LR
    }]
    optimizer = optim.SGD(trainable_param, momentum=cfg.PRUNING.MOMENTUM, weight_decay=cfg.PRUNING.WEIGHT_DECAY)
    lr_scheduler = build_lr_scheduler(optimizer, epochs=cfg.PRUNING.EPOCHS,config=cfg.PRUNING.LR)
    lr_scheduler.step(cfg.PRUNING.START_EPOCH)
    return optimizer, lr_scheduler


def build_data_loader():
    logger.info("build train dataset")
    # train_dataset
    train_dataset = TrainDataset()
    logger.info("build dataset done")

    train_dataloader = DataLoader(train_dataset,
                                  batch_size=cfg.PRUNING.BATCH_SIZE,
                                  num_workers=cfg.TRAIN.NUM_WORKERS,
                                  pin_memory=True)
    return train_dataloader

def train(train_dataloader, model, optimizer, lr_scheduler):
    def is_valid_number(x):
        return not (math.isnan(x) or math.isinf(x) or x > 1e4)

    logger.info("model\n{}".format(describe(model)))
    tb_writer = SummaryWriter(cfg.PRUNING.LOG_DIR)
    average_meter = AverageMeter()
    start_epoch = cfg.PRUNING.START_EPOCH
    num_per_epoch = len(train_dataloader.dataset) // (cfg.PRUNING.BATCH_SIZE)
    iter = 0
    if not os.path.exists(cfg.PRUNING.SNAPSHOT_DIR):
        os.makedirs(cfg.PRUNING.SNAPSHOT_DIR)
    model.apply_mask() # apply the mask when resume, if start in 0 epoch, the mask are all 1
    for epoch in range(cfg.PRUNING.START_EPOCH, cfg.PRUNING.EPOCHS):
        train_dataloader.dataset.shuffle()
        lr_scheduler.step(epoch)
        # log for lr
        for idx, pg in enumerate(optimizer.param_groups):
            tb_writer.add_scalar('lr/group{}'.format(idx + 1), pg['lr'], iter)
        cur_lr = lr_scheduler.get_cur_lr()
        for data in train_dataloader:
            begin = time.time()
            examplar_img = data['examplar_img'].cuda()
            search_img = data['search_img'].cuda()
            gt_cls = data['gt_cls'].cuda()
            gt_delta = data['gt_delta'].cuda()
            delta_weight = data['delta_weight'].cuda()
            data_time = time.time() - begin
            losses = model.forward(examplar_img, search_img, gt_cls, gt_delta, delta_weight)
            cls_loss = losses['cls_loss']
            loc_loss = losses['loc_loss']
            loss = losses['total_loss']

            if is_valid_number(loss.item()):
                optimizer.zero_grad()
                loss.backward()
                if cfg.PRUNING.LOG_GRAD:
                    log_grads(model.module, tb_writer, iter)
                clip_grad_norm_(model.parameters(), cfg.PRUNING.GRAD_CLIP)
                optimizer.step()

            batch_time = time.time() - begin
            batch_info = {}
            batch_info['data_time'] = data_time
            batch_info['batch_time'] = batch_time
            for k, v in losses.items():
                batch_info[k] = v
            average_meter.update(**batch_info)
            for k, v in batch_info.items():
                tb_writer.add_scalar(k, v, iter)

            if iter % cfg.TRAIN.PRINT_EVERY == 0:
                logger.info('epoch: {}, iter: {}, cur_lr:{}, cls_loss: {}, loc_loss: {}, loss: {}'
                            .format(epoch + 1, iter, cur_lr, cls_loss.item(), loc_loss.item(), loss.item()))
                print_speed(iter + 1 + start_epoch * num_per_epoch,
                            average_meter.batch_time.avg,
                            cfg.PRUNING.EPOCHS * num_per_epoch)
                # mem = psutil.virtual_memory()
                # mem_used=mem.used/1024/1024
                # logger.info('memory used: {}M'.format(mem_used))
            iter += 1
        model.update_mask()
        model.apply_mask()
        for k, v in model.mask.items():
            tb_writer.add_histogram('mask.' + k, v, iter)
        for k, v in model.mask_scores.items():
            tb_writer.add_histogram('mask_score.' + k, v, iter)
        # save model
        state = {
            'model': model.state_dict(),
            'optimizer': optimizer.state_dict(),
            'epoch': epoch + 1,
            'mask': model.mask,
            'mask_scores': model.mask_scores
        }
        logger.info('save snapshot to {}/checkpoint_e{}.pth'.format(cfg.PRUNING.SNAPSHOT_DIR, epoch + 1))
        torch.save(state, '{}/checkpoint_e{}.pth'.format(cfg.PRUNING.SNAPSHOT_DIR, epoch + 1))


def main():
    cfg.merge_from_file(args.cfg)
    if not os.path.exists(cfg.PRUNING.LOG_DIR):
        os.makedirs(cfg.PRUNING.LOG_DIR)
    init_log('global', logging.INFO)
    if cfg.PRUNING.LOG_DIR:
        add_file_handler('global',
                         os.path.join(cfg.PRUNING.LOG_DIR, 'logs.txt'),
                         logging.INFO)
    logger.info("Version Information: \n{}\n".format(commit()))
    logger.info("config \n{}".format(json.dumps(cfg, indent=4)))

    train_dataloader = build_data_loader()
    model = PruningSiamModel().cuda().train()
    optimizer, lr_scheduler = build_optimizer_lr(model, cfg.PRUNING.START_EPOCH)
    logger.info('load pretrain from {}.'.format(cfg.PRUNING.PRETRAIN_PATH))
    model = load_pretrain(model, cfg.PRUNING.PRETRAIN_PATH)
    logger.info('load pretrain done')
    if cfg.PRUNING.RESUME:
        logger.info('resume from {}'.format(cfg.PRUNING.RESUME_PATH))
        model, optimizer, cfg.PRUNING.START_EPOCH = restore_from(model, optimizer, cfg.PRUNING.RESUME_PATH)
        logger.info('resume done!')
    train(train_dataloader, model, optimizer, lr_scheduler)


if __name__ == '__main__':
    seed_torch(123456)
    main()
