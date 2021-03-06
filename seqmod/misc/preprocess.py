
import os
import time
import warnings

import numpy as np
import pickle as p


def segmenter(sent, level='char'):
    if level == 'char':
        return list(sent)
    elif level == 'token':
        return sent.split()
    else:
        raise ValueError


def text_processor(language='en', num=False, lower=False, level='token'):
    try:
        from normalizr import Normalizr
    except ImportError:
        try:
            from cucco import Cucco as Normalizr
        except ImportError:
            warnings.warn("Try installing normalizr or cucco")
            return lambda sent: sent

    normalizations = [
        ('replace_emails', {'replacement': '<email>'}),
        ('replace_emojis', {'replacement': '<emoji>'}),
        ('replace_urls', {'replacement': '<url>'})]
    normalizr = Normalizr()

    import re
    NUM = re.compile('[0-9]+')

    def processor(sent):
        sent = normalizr.normalize(sent, normalizations)
        if num:
            sent = NUM.sub('<num>', sent)  # number substitution
        if lower:
            sent = sent.lower()  # downcase
        return segmenter(sent, level=level)

    return processor


def process_files(files, processor, max_buffer_size):
    for f in files:
        with open(f, 'r') as lines:
            to_process = []
            for line in lines:
                to_process.append(processor(line.strip()))
                if len(to_process) >= max_buffer_size:
                    yield to_process
                    to_process = []
            if len(to_process) > 0:
                yield to_process


if __name__ == '__main__':
    from seqmod.misc.dataset import Dict

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('path')
    parser.add_argument('--output_file', type=str, default="processed")
    parser.add_argument('--min_freq', type=int, default=1)
    parser.add_argument('--max_size', type=int, default=None)
    parser.add_argument('--bos_token', type=str, default='<bos>')
    parser.add_argument('--eos_token', type=str, default='<eos>')
    parser.add_argument('--max_buffer_size', type=int, default=100000)
    parser.add_argument('--num', action='store_true')
    parser.add_argument('--lower', action='store_true')
    parser.add_argument('--level', default='token')
    args = parser.parse_args()

    extractor = Dict(
        max_size=args.max_size, min_freq=args.min_freq,
        bos_token=args.bos_token, eos_token=args.eos_token)

    processor = text_processor(
        num=args.num, lower=args.lower, level=args.level)

    if os.path.isfile(args.path):
        files = [args.path]
    else:
        files = [os.path.join(args.path, f) for f in os.listdir(args.path)]

    start = time.time()
    print("Fitting vocabulary")
    for subset in process_files(files, processor, args.max_buffer_size):
        extractor.partial_fit(subset)
    extractor.fit()
    print(" * Vocabulary size: %d" % len(extractor))

    print("Transforming data")
    tokens = []
    for subset in process_files(files, processor, args.max_buffer_size):
        for line in extractor.transform(subset):
            tokens.extend(line)
    np.save(args.output_file + ".corpus.npy", np.array(tokens, dtype=np.int32))
    print("* Corpus size: %d" % len(tokens))

    print("Saving dictonary")
    with open(args.output_file + ".dict.pickle", "wb+") as f:
        p.dump(extractor, f)

    print("Done in %d seconds" % int(time.time() - start))
