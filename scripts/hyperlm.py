
import math
import os
import warnings

import random; random.seed(1001)

import torch
try:
    torch.cuda.manual_seed(1001)
except:
    warnings.warn('no NVIDIA driver found')
    torch.manual_seed(1001)

from seqmod.hyper import Hyperband
from seqmod.hyper.utils import make_sampler

from seqmod.modules.lm import LM
from seqmod import utils as u
from seqmod.misc.trainer import Trainer
from seqmod.misc.loggers import StdLogger
from seqmod.misc.optimizer import Optimizer
from seqmod.misc.dataset import Dict, BlockDataset
from seqmod.misc.preprocess import text_processor
from seqmod.misc.early_stopping import EarlyStopping


# Load data
def load_lines(path, processor=text_processor()):
    lines = []
    if os.path.isfile(path):
        input_files = [path]
    else:
        input_files = [os.path.join(path, f) for f in os.listdir(path)]
    for path in input_files:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if processor is not None:
                    line = processor(line)
                if line:
                    lines.append(line)
    return lines


def load_from_file(path):
    if path.endswith('npy'):
        import numpy as np
        array = np.load(path).astype(np.int64)
        data = torch.LongTensor(array)
    elif path.endswith('.pt'):
        data = torch.load(path)
    else:
        raise ValueError('Unknown input format [%s]' % path)
    return data


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    # dataset
    parser.add_argument('--path', required=True)
    parser.add_argument('--processed', action='store_true')
    parser.add_argument('--dict_path', type=str)
    parser.add_argument('--max_size', default=1000000, type=int)
    parser.add_argument('--min_freq', default=1, type=int)
    parser.add_argument('--lower', action='store_true')
    parser.add_argument('--num', action='store_true')
    parser.add_argument('--level', default='token')
    parser.add_argument('--dev_split', default=0.1, type=float)
    parser.add_argument('--test_split', default=0.1, type=float)
    # training
    parser.add_argument('--batch_size', default=20, type=int)
    parser.add_argument('--bptt', default=20, type=int)
    parser.add_argument('--gpu', action='store_true')
    # - optimizer
    parser.add_argument('--optim', default='Adam', type=str)
    parser.add_argument('--lr', default=0.01, type=float)
    parser.add_argument('--max_norm', default=5., type=float)
    parser.add_argument('--early_stopping', default=-1, type=int)
    # - hyperopts
    parser.add_argument('--max_iter', default=81, type=int)
    parser.add_argument('--eta', default=3, type=int)
    # - check
    parser.add_argument('--seed', default=None)
    parser.add_argument('--decoding_method', default='sample')
    parser.add_argument('--max_seq_len', default=25, type=int)
    parser.add_argument('--temperature', default=1, type=float)
    parser.add_argument('--checkpoint', default=200, type=int)
    parser.add_argument('--hooks_per_epoch', default=5, type=int)
    parser.add_argument('--log_checkpoints', action='store_true')
    parser.add_argument('--visdom', action='store_true')
    parser.add_argument('--visdom_host', default='localhost')
    parser.add_argument('--save', action='store_true')
    parser.add_argument('--save_path', default='models', type=str)
    args = parser.parse_args()

    # process data
    if args.processed:
        print("Loading preprocessed datasets...")
        assert args.dict_path, "Processed data requires DICT_PATH"
        data, d = load_from_file(args.path), u.load_model(args.dict_path)
        train, test, valid = BlockDataset(
            data, d, args.batch_size, args.bptt, gpu=args.gpu, fitted=True
        ).splits(test=args.test_split, dev=args.dev_split)
        del data
    else:
        print("Processing datasets...")
        proc = text_processor(lower=args.lower, num=args.num, level=args.level)
        d = Dict(max_size=args.max_size, min_freq=args.min_freq,
                 eos_token=u.EOS, force_unk=True)
        train, valid, test = None, None, None
        # already split
        if os.path.isfile(os.path.join(args.path, 'train.txt')):
            if not os.path.isfile(os.path.join(args.path, 'valid.txt')):
                raise ValueError("train.txt requires test.txt")
            train_data = load_lines(
                os.path.join(args.path, 'train.txt'), processor=proc)
            d.fit(train_data)
            train = BlockDataset(
                train_data, d, args.batch_size, args.bptt, gpu=args.gpu)
            del train_data
            test = BlockDataset(
                load_lines(os.path.join(args.path, 'test.txt'), proc),
                d, args.batch_size, args.bptt, gpu=args.gpu, evaluation=True)
            if os.path.isfile(os.path.join(args.path, 'valid.txt')):
                valid = BlockDataset(
                    load_lines(
                        os.path.join(args.path, 'valid.txt'), processor=proc),
                    d, args.batch_size, args.bptt, gpu=args.gpu,
                    evaluation=True)
            else:
                train, valid = train.splits(dev=None, test=args.dev_split)
        # do split, assume input is single file or dir with txt files
        else:
            data = load_lines(args.path, processor=proc)
            d.fit(data)
            train, valid, test = BlockDataset(
                data, d, args.batch_size, args.bptt, gpu=args.gpu
            ).splits(test=args.test_split, dev=args.dev_split)
            del data

    print(' * vocabulary size. %d' % len(d))
    print(' * number of train batches. %d' % len(train))

    # prepare hyperband functions
    sampler = make_sampler({
        'emb_dim': ['uniform', int, 20, 50],
        'hid_dim': ['uniform', int, 100, 1000],
        'num_layers': ['choice', int, (1, 2)],
        'dropout': ['loguniform', float, math.log(0.1), math.log(0.5)],
        'word_dropout': ['loguniform', float, math.log(0.1), math.log(0.5)],
        'cell': ['choice', str, ('GRU', 'LSTM', 'RHN')],
        'train_init': ['choice', bool, (True, False)],
        'deepout_layers': ['uniform', int, 1, 3],
        'maxouts': ['uniform', int, 1, 4]
        # 'lr': ['loguniform', float, math.log(0.001), math.log(0.05)]
    })

    class create_runner(object):
        def __init__(self, params):
            self.trainer, self.early_stopping = None, None

            m = LM(len(d), params['emb_dim'], params['hid_dim'],
                   num_layers=params['num_layers'], cell=params['cell'],
                   dropout=params['dropout'], train_init=params['train_init'],
                   deepout_layers=params['deepout_layers'],
                   maxouts=params['maxouts'], target_code=d.get_unk(),
                   word_dropout=params['word_dropout'])
            u.initialize_model(m)

            optim = Optimizer(
                m.parameters(), args.optim, lr=args.lr, max_norm=args.max_norm)

            self.early_stopping = EarlyStopping(
                5, patience=3, reset_patience=False)

            def early_stop_hook(trainer, epoch, batch_num, num_checkpoints):
                valid_loss = trainer.validate_model()
                self.early_stopping.add_checkpoint(sum(valid_loss.pack()))

            trainer = Trainer(
                m, {"train": train, "test": test, "valid": valid}, optim)
            trainer.add_hook(early_stop_hook, hooks_per_epoch=5)
            trainer.add_loggers(StdLogger())

            self.trainer = trainer

        def __call__(self, n_iters):
            # max run will be 5 epochs
            batches = len(self.trainer.datasets['train'])
            batches = int((batches / args.max_iter) * 5)

            if args.gpu:
                self.trainer.model.cuda()
            (_, loss), _ = self.trainer.train_batches(batches * n_iters, 10)
            self.trainer.model.cpu()

            return {'loss': loss, 'early_stop': self.early_stopping.stopped}

    hb = Hyperband(
        sampler, create_runner, max_iter=args.max_iter, eta=args.eta)

    print(hb.run())
