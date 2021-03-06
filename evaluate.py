#!/usr/bin/python
# -*- encoding: utf-8 -*-


import sys
import os
import os.path as osp
import logging
import pickle
from tqdm import tqdm
import numpy as np
import torch
from backbone import Embeddor
from loss import BottleneckLoss
from torch.utils.data import DataLoader
from market1501 import Market1501
from sft import SFT_np



FORMAT = '%(levelname)s %(filename)s(%(lineno)d): %(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT, stream=sys.stdout)
logger = logging.getLogger(__name__)


def embed():
    ## load checkpoint
    res_pth = './res'
    mod_pth = osp.join(res_pth, 'model_final.pth')
    states = torch.load(mod_pth)
    net = Embeddor()
    net.load_state_dict(states)
    net.cuda()
    net.eval()

    ## data loader
    query_set = Market1501('./dataset/Market-1501-v15.09.15/query',
            is_train = False)
    gallery_set = Market1501('./dataset/Market-1501-v15.09.15/bounding_box_test',
            is_train = False)
    query_loader = DataLoader(query_set,
                        batch_size = 32,
                        num_workers = 4,
                        drop_last = False)
    gallery_loader = DataLoader(gallery_set,
                        batch_size = 32,
                        num_workers = 4,
                        drop_last = False)

    ## embed
    logger.info('embedding query set ...')
    query_pids = []
    query_camids = []
    query_embds = []
    for i, (im, _, ids) in enumerate(tqdm(query_loader)):
        embds = []
        with torch.no_grad():
            for crop in im:
                crop = crop.cuda()
                emb = net(crop)[0]
                embds.append(emb.detach().cpu().numpy())
        embed = sum(embds) / len(embds)
        pid = ids[0].numpy()
        camid = ids[1].numpy()
        query_embds.append(embed)
        query_pids.extend(pid)
        query_camids.extend(camid)
    query_embds = np.vstack(query_embds)
    query_pids = np.array(query_pids)
    query_camids = np.array(query_camids)

    logger.info('embedding gallery set ...')
    gallery_pids = []
    gallery_camids = []
    gallery_embds = []
    for i, (im, _, ids) in enumerate(tqdm(gallery_loader)):
        embds = []
        with torch.no_grad():
            for crop in im:
                crop = crop.cuda()
                emb = net(crop)[0]
                embds.append(emb.detach().cpu().numpy())
        embed = sum(embds) / len(embds)
        pid = ids[0].numpy()
        camid = ids[1].numpy()
        gallery_embds.append(embed)
        gallery_pids.extend(pid)
        gallery_camids.extend(camid)
    gallery_embds = np.vstack(gallery_embds)
    gallery_pids = np.array(gallery_pids)
    gallery_camids = np.array(gallery_camids)

    ## dump embeds results
    embd_res = (query_embds, query_pids, query_camids, gallery_embds, gallery_pids, gallery_camids)
    with open('./res/embds.pkl', 'wb') as fw:
        pickle.dump(embd_res, fw)
    logger.info('embedding done, dump to: ./res/embds.pkl')

    return embd_res



def evaluate(embd_res, cmc_max_rank=1, post_top_n=None):
    sft_op = SFT_np(sigma=0.1)
    query_embds, query_pids, query_camids, gallery_embds, gallery_pids, gallery_camids = embd_res
    query_embds_norm = np.linalg.norm(query_embds, 2, 1).reshape(-1, 1)
    query_embds = query_embds / query_embds_norm
    gallery_embds_norm = np.linalg.norm(gallery_embds, 2, 1).reshape(-1, 1)
    gallery_embds = gallery_embds / gallery_embds_norm

    ## compute distance matrix
    logger.info('compute distance matrix')
    dist_mtx = np.matmul(query_embds, gallery_embds.T)
    dist_mtx = 1.0 / (dist_mtx + 1)
    n_q, n_g = dist_mtx.shape

    logger.info('start evaluating ...')
    indices = np.argsort(dist_mtx, axis = 1)
    matches = gallery_pids[indices] == query_pids[:, np.newaxis]
    matches = matches.astype(np.int32)
    all_aps = []
    all_cmcs = []
    for query_idx in tqdm(range(n_q)):
        qemb = query_embds[query_idx].reshape((1, -1))
        query_pid = query_pids[query_idx]
        query_camid = query_camids[query_idx]

        ## exclude duplicated gallery pictures
        order = indices[query_idx]
        pid_diff = gallery_pids[order] != query_pid
        camid_diff = gallery_camids[order] != query_camid
        useful = gallery_pids[order] != -1
        keep = np.logical_or(pid_diff, camid_diff)
        keep = np.logical_and(keep, useful)
        match = matches[query_idx][keep]

        if not np.any(match): continue
        ## post processing
        if not post_top_n == None:
            gallery_keep = gallery_embds[order][keep]
            gallery_top_n = gallery_keep[:post_top_n]
            gallery_top_n_sft = sft_op(gallery_top_n)
            cosine = np.matmul(qemb, gallery_top_n_sft.T)
            sft_order = np.argsort(-cosine)
            match[:post_top_n] = match[:post_top_n][sft_order]

        ## compute cmc
        cmc = match.cumsum()
        cmc[cmc > 1] = 1
        all_cmcs.append(cmc[:cmc_max_rank])

        ## compute map
        num_real = match.sum()
        match_cum = match.cumsum()
        match_cum = [el / (1.0 + i) for i, el in enumerate(match_cum)]
        match_cum = np.array(match_cum) * match
        ap = match_cum.sum() / num_real
        all_aps.append(ap)

    assert len(all_aps) > 0, "NO QUERRY APPEARS IN THE GALLERY"
    mAP = sum(all_aps) / len(all_aps)
    all_cmcs = np.array(all_cmcs, dtype = np.float32)
    cmc = np.mean(all_cmcs, axis = 0)

    return cmc, mAP


if __name__ == '__main__':
    embd_res = embed()
    with open('./res/embds.pkl', 'rb') as fr:
        embd_res = pickle.load(fr)
    cmc, mAP = evaluate(embd_res, post_top_n=None)
    print('without post_processing: cmc is: {}, map is: {}'.format(cmc, mAP))
    cmc, mAP = evaluate(embd_res, post_top_n=50)
    print('with post_processing: cmc is: {}, map is: {}'.format(cmc, mAP))
