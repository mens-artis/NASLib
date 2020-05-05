import logging
import os
import random

import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torchvision.datasets as dset
import yaml

from naslib.optimizers.optimizer import DARTSOptimizer
from naslib.search_spaces.core.graph_darts import DARTSMacroGraph
from naslib.utils import AttrDict
from naslib.utils import utils


class Evaluator(object):
    def __init__(self, graph, *args, **kwargs):
        self.graph = graph
        try:
            self.config = kwargs.get('config', graph.config)
        except:
            raise ('No configuration specified in graph or kwargs')

        np.random.seed(self.config.seed)
        random.seed(self.config.seed)
        torch.manual_seed(self.config.seed)
        torch.cuda.set_device(self.config.gpu)
        cudnn.benchmark = False
        cudnn.enabled = True
        cudnn.deterministic = True
        torch.cuda.manual_seed_all(self.config.seed)

        #TODO: move all the data loading and preproces inside another method
        train_transform, valid_transform = utils._data_transforms_cifar10(config)
        self.train_transform = train_transform
        self.valid_transform = valid_transform
        train_data = dset.CIFAR10(root=config.data, train=True, download=True, transform=train_transform)
        test_data = dset.CIFAR10(root=config.data, train=False, download=True, transform=valid_transform)

        num_train = len(train_data)
        indices = list(range(num_train))
        split = int(np.floor(self.config.train_portion * num_train))

        self.train_queue = torch.utils.data.DataLoader(
            train_data, batch_size=self.config.batch_size,
            sampler=torch.utils.data.sampler.SubsetRandomSampler(indices[:split]),
            pin_memory=True, num_workers=0,
            worker_init_fn=np.random.seed(self.config.seed))

        self.valid_queue = torch.utils.data.DataLoader(
            train_data, batch_size=self.config.batch_size,
            sampler=torch.utils.data.sampler.SubsetRandomSampler(indices[split:num_train]),
            pin_memory=True, num_workers=0,
            worker_init_fn=np.random.seed(self.config.seed))

        self.test_queue = torch.utils.data.DataLoader(
            test_data, batch_size=self.config.batch_size,
            shuffle=False, pin_memory=True, num_workers=0)

        criterion = eval('nn.' + self.config.criterion)()
        self.criterion = criterion.cuda()

        self.model = graph.cuda()

        logging.info("param size = %fMB", utils.count_parameters_in_MB(self.model))

        optimizer = torch.optim.SGD(
            self.model.parameters(),
            self.config.learning_rate,
            momentum=self.config.momentum,
            weight_decay=self.config.weight_decay)
        self.optimizer = optimizer

        self.scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer, float(self.config.epochs), eta_min=self.config.learning_rate_min)

        logging.info('Args: {}'.format(self.config))

    def run(self, *args, **kwargs):
        if 'epochs' not in kwargs:
            epochs = self.config.epochs
        else:
            raise ('No number of epochs specified to run network')

        for epoch in range(epochs):
            logging.info('epoch %d lr %e', epoch, self.scheduler.get_lr()[0])
            self.model.drop_path_prob = self.config.drop_path_prob * epoch / epochs

            train_acc, train_obj = Evaluator.train(self.model, self.optimizer, self.criterion, self.train_queue)
            logging.info('train_acc %f', train_acc)

            if len(self.valid_queue) != 0:
                valid_acc, valid_obj = Evaluator.infer(self.model, self.criterion, self.valid_queue)
                logging.info('valid_acc %f', valid_acc)

            test_acc, test_obj = Evaluator.infer(self.model, self.criterion, self.test_queue)
            logging.info('test_acc %f', test_acc)

            Evaluator.save(self.config.save, self.model, epoch)

    @staticmethod
    def train(graph, optimizer, criterion, train_queue, *args, **kwargs):
        try:
            config = config if 'config' in kwargs else graph.config
        except:
            raise ('No configuration specified in graph or kwargs')

        objs = utils.AvgrageMeter()
        top1 = utils.AvgrageMeter()
        top5 = utils.AvgrageMeter()

        for step, (input, target) in enumerate(train_queue):
            graph.train()
            n = input.size(0)

            input = input.cuda()
            target = target.cuda(non_blocking=True)

            # if architect in kwargs:
            # get a minibatch from the search queue with replacement
            #    input_search, target_search = next(iter(valid_queue))

            #    input_search = input_search.cuda()
            #    target_search = target_search.cuda(non_blocking=True)

            # Allow for warm starting of the one-shot model for more reliable architecture updates.
            #    if epoch >= self.args.warm_start_epochs:
            #        architect.step(input_train=input,
            #                       target_train=target,
            #                       input_valid=input_search,
            #                       target_valid=target_search,
            #                       eta=lr,
            #                       network_optimizer=self.optimizer,
            #                       unrolled=self.args.unrolled)

            optimizer.zero_grad()
            logits, logits_aux = graph(input)
            loss = criterion(logits, target)
            if config.auxiliary:
                loss_aux = criterion(logits_aux, target)
                loss += config.auxiliary_weight * loss_aux
            loss.backward()
            nn.utils.clip_grad_norm_(graph.parameters(), config.grad_clip)
            optimizer.step()

            prec1, prec5 = utils.accuracy(logits, target, topk=(1, 5))
            objs.update(loss.data.item(), n)
            top1.update(prec1.data.item(), n)
            top5.update(prec5.data.item(), n)

            if step % config.report_freq == 0:
                logging.info('train %03d %e %f %f', step, objs.avg, top1.avg, top5.avg)

        return top1.avg, objs.avg

    @staticmethod
    def infer(graph, criterion, valid_queue, *args, **kwargs):
        try:
            config = kwargs.get('config', graph.config)
        except:
            raise ('No configuration specified in graph or kwargs')

        objs = utils.AvgrageMeter()
        top1 = utils.AvgrageMeter()
        top5 = utils.AvgrageMeter()
        graph.eval()

        with torch.no_grad():
            for step, (input, target) in enumerate(valid_queue):
                input = input.cuda()
                target = target.cuda(non_blocking=True)

                logits, _ = graph(input)
                loss = criterion(logits, target)

                prec1, prec5 = utils.accuracy(logits, target, topk=(1, 5))
                n = input.size(0)
                objs.update(loss.data.item(), n)
                top1.update(prec1.data.item(), n)
                top5.update(prec5.data.item(), n)

                if step % config.report_freq == 0:
                    logging.info('valid %03d %e %f %f', step, objs.avg, top1.avg, top5.avg)

        return top1.avg, objs.avg

    @staticmethod
    def save(save_path, model, epoch):
        utils.save(model, os.path.join(save_path,
                                       'one_shot_model_{}.pt'.format(epoch)))

if __name__ == '__main__':
    import yaml
    from naslib.search_spaces.core.graph_darts import DARTSMacroGraph
    from naslib.optimizers.optimizer import Optimizer
    from naslib.utils import AttrDict

    with open('../configs/default.yaml') as f:
        config = yaml.safe_load(f)
        config = AttrDict(config)

    one_shot_optimizer = Optimizer()
    search_space = DARTSMacroGraph.from_optimizer_op(one_shot_optimizer,
                                                     config=config)

    evaluator = Evaluator(search_space)
    evaluator.run()