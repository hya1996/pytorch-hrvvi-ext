from toolz import curry

from torch.utils.data.dataloader import default_collate

import torch
import torch.nn as nn
import torch.nn.functional as F

from horch.common import select, _concat
from horch.detection.one import MultiBoxLoss
from horch.detection.bbox import BBox
from horch.detection.iou import iou_mn
from horch.detection.nms import nms, soft_nms_cpu


def coords_to_target(gt_box, anchors):
    box_txty = (gt_box[..., :2] - anchors[..., :2]) / anchors[..., 2:]
    box_twth = (gt_box[..., 2:] / anchors[..., 2:]).log_()
    return torch.cat((box_txty, box_twth), dim=-1)


def coords_to_target2(bboxes, anchors):
    r"""
    Parameters
    ----------
    bboxes
        (n, 4)
    anchors
        (m, 4)
    """
    bboxes = bboxes[:, None, :]
    anchors = anchors[None, :, :]
    txty = (bboxes[..., :2] - anchors[..., :2]) / anchors[..., 2:]
    twth = (bboxes[..., 2:] / anchors[..., 2:]).log_()
    return torch.cat((txty, twth), dim=-1)


def target_to_coords(loc_t, anchors):
    loc_t[..., :2].mul_(anchors[:, 2:]).add_(anchors[:, :2])
    loc_t[..., 2:].exp_().mul_(anchors[:, 2:])
    return loc_t


def match_anchors2(anns, a_xywh, a_ltrb, pos_thresh=0.5, neg_thresh=None,
                       get_label=lambda x: x['category_id'], debug=False):
    num_anchors = len(a_xywh)

    if len(anns) == 0:
        loc_t = a_xywh.new_zeros(num_anchors, 4)
        cls_t = loc_t.new_zeros(num_anchors, dtype=torch.long)
        target = [loc_t, cls_t]
        if neg_thresh:
            target.append(loc_t.new_zeros(num_anchors, dtype=torch.uint8))
        return target

    bboxes = a_xywh.new_tensor([ann['bbox'] for ann in anns])
    bboxes = BBox.convert(bboxes, format=BBox.LTWH, to=BBox.XYWH, inplace=True)
    labels = a_xywh.new_tensor([get_label(ann) for ann in anns], dtype=torch.long)

    bboxes_ltrb = BBox.convert(bboxes, BBox.XYWH, BBox.LTRB)
    ious = iou_mn(bboxes_ltrb, a_ltrb)

    pos = ious > pos_thresh
    cls_t, indices = (pos.long() * labels[:, None]).max(dim=0)
    loc_t_all = coords_to_target2(bboxes, a_xywh)
    loc_t = select(loc_t_all, 0, indices)

    max_ious, max_indices = ious.max(dim=1)
    if debug:
        print(max_ious.tolist())
    loc_t[max_indices] = select(loc_t_all, 1, max_indices)
    cls_t[max_indices] = labels

    target = [loc_t, cls_t]

    if neg_thresh:
        ignore = (cls_t == 0) & ((ious >= neg_thresh).sum(dim=0) != 0)
        target.append(ignore)
    return target


@curry
def match_anchors(anns, a_xywh, a_ltrb, pos_thresh=0.5, neg_thresh=None,
                       get_label=lambda x: x['category_id'], debug=False):
    num_anchors = len(a_xywh)
    loc_t = a_xywh.new_zeros(num_anchors, 4)
    cls_t = loc_t.new_zeros(num_anchors, dtype=torch.long)

    if len(anns) == 0:
        target = [loc_t, cls_t]
        if neg_thresh:
            target.append(loc_t.new_zeros(num_anchors, dtype=torch.uint8))
        return target

    bboxes = loc_t.new_tensor([ann['bbox'] for ann in anns])
    bboxes = BBox.convert(bboxes, format=BBox.LTWH, to=BBox.XYWH, inplace=True)
    labels = loc_t.new_tensor([get_label(ann) for ann in anns], dtype=torch.long)

    bboxes_ltrb = BBox.convert(bboxes, BBox.XYWH, BBox.LTRB)
    ious = iou_mn(bboxes_ltrb, a_ltrb)

    pos = ious > pos_thresh
    for ipos, bbox, label in zip(pos, bboxes, labels):
        loc_t[ipos] = coords_to_target(bbox, a_xywh[ipos])
        cls_t[ipos] = label

    max_ious, indices = ious.max(dim=1)
    if debug:
        print(max_ious.tolist())
    loc_t[indices] = coords_to_target(bboxes, a_xywh[indices])
    cls_t[indices] = labels

    target = [loc_t, cls_t]

    if neg_thresh:
        ignore = (cls_t == 0) & ((ious >= neg_thresh).sum(dim=0) != 0)
        target.append(ignore)
    return target


def match_rois(rois, anns, pos_thresh=0.5, n_samples=64, pos_neg_ratio=1/3, mask_size=None):
    rois_xywh = BBox.convert(rois, BBox.LTRB, BBox.XYWH)
    num_anns = len(anns)
    num_rois = len(rois)
    loc_t = rois.new_zeros(num_rois, 4)
    cls_t = loc_t.new_zeros(num_rois, dtype=torch.long)

    if num_anns == 0:
        return loc_t, cls_t

    bboxes = loc_t.new_tensor([ann['bbox'] for ann in anns])
    bboxes = BBox.convert(bboxes, format=BBox.LTWH, to=BBox.XYWH, inplace=True)
    labels = loc_t.new_tensor([ann['category_id'] for ann in anns], dtype=torch.long)

    bboxes_ltrb = BBox.convert(bboxes, BBox.XYWH, BBox.LTRB)
    ious = iou_mn(bboxes_ltrb, rois)

    if mask_size:
        ann_indices = torch.zeros(num_rois, dtype=torch.long)
    max_ious, indices = ious.max(dim=1)
    loc_t[indices] = coords_to_target(bboxes, rois_xywh[indices])
    cls_t[indices] = labels
    if mask_size:
        ann_indices[indices] = torch.arange(num_rois)

    pos = ious > pos_thresh
    for ann_id, ipos, bbox, label in zip(range(num_rois), pos, bboxes, labels):
        loc_t[ipos] = coords_to_target(bbox, rois_xywh[ipos])
        cls_t[ipos] = label
        if mask_size:
            ann_indices[ipos] = ann_id

    pos = cls_t != 0
    n_pos = int(n_samples * pos_neg_ratio / (pos_neg_ratio + 1))
    n_neg = n_samples - n_pos
    pos_indices = sample(torch.nonzero(pos).squeeze(1), n_pos)
    neg_indices = sample(torch.nonzero(~pos).squeeze(1), n_neg)
    loc_t = loc_t[pos_indices]
    indices = torch.cat([pos_indices, neg_indices], dim=0)
    cls_t = cls_t[indices]

    if mask_size:
        mask_t = loc_t.new_zeros(n_pos, *mask_size)
        for i in range(n_pos):
            ind = pos_indices[i]
            mask = anns[ann_indices[ind]]
            l, t, r, b = rois[ind]
            l = max(0, int(l))
            t = max(0, int(t))
            r = int(r)
            b = int(b)
            m = mask[t:b, l:r].float()
            m = m.view(1, 1, *m.size())
            m = F.interpolate(m, size=mask_size).squeeze()
            mask_t[i] = m
        return loc_t, cls_t, mask_t, indices
    else:
        return loc_t, cls_t, indices


def match_rois2(anns, rois, pos_thresh=0.5, n_samples=64, pos_neg_ratio=1/3, mask_size=None):
    num_rois = len(rois)
    if len(anns) == 0:
        loc_t = rois.new_zeros(num_rois, 4)
        cls_t = loc_t.new_zeros(num_rois, dtype=torch.long)
        return loc_t, cls_t

    rois_xywh = BBox.convert(rois, BBox.LTRB, BBox.XYWH)

    bboxes = rois.new_tensor([ann['bbox'] for ann in anns])
    bboxes = BBox.convert(bboxes, format=BBox.LTWH, to=BBox.XYWH, inplace=True)
    labels = rois.new_tensor([ann['category_id'] for ann in anns], dtype=torch.long)

    bboxes_ltrb = BBox.convert(bboxes, BBox.XYWH, BBox.LTRB)
    ious = iou_mn(bboxes_ltrb, rois)

    pos = ious > pos_thresh
    cls_t, ann_indices = (pos.long() * labels[:, None]).max(dim=0)
    loc_t_all = coords_to_target2(bboxes, rois_xywh)
    loc_t = select(loc_t_all, 0, ann_indices)

    max_ious, max_indices = ious.max(dim=1)
    loc_t[max_indices] = select(loc_t_all, 1, max_indices)
    cls_t[max_indices] = labels

    ann_indices[max_indices] = torch.arange(len(anns), device=rois.device)

    pos = cls_t != 0
    n_pos = int(n_samples * pos_neg_ratio / (pos_neg_ratio + 1))
    n_neg = n_samples - n_pos
    pos_indices = sample(torch.nonzero(pos).squeeze(1), n_pos)
    neg_indices = sample(torch.nonzero(~pos).squeeze(1), n_neg)
    loc_t = loc_t[pos_indices]
    indices = torch.cat([pos_indices, neg_indices], dim=0)
    cls_t = cls_t[indices]

    if mask_size:
        mask_t = loc_t.new_zeros(n_pos, *mask_size)
        for i in range(n_pos):
            ind = pos_indices[i]
            mask = anns[ann_indices[ind]]
            l, t, r, b = rois[ind]
            l = max(0, int(l))
            t = max(0, int(t))
            r = int(r)
            b = int(b)
            m = mask[t:b, l:r].float()
            m = m.view(1, 1, *m.size())
            m = F.interpolate(m, size=mask_size).squeeze()
            mask_t[i] = m
        return loc_t, cls_t, mask_t, indices
    else:
        return loc_t, cls_t, indices


def sample(t, n):
    if len(t) >= n:
        indices = torch.randperm(len(t), device=t.device)[:n]
    else:
        indices = torch.randint(len(t), size=(n,), device=t.device)
    return t[indices]


def flatten(xs):
    if torch.is_tensor(xs):
        return xs.view(-1, xs.size(-1))
    xs = [x.view(-1, x.size(-1)) for x in xs]
    return _concat(xs, dim=0)


class MatchAnchors:
    r"""

    Parameters
    ----------
    anchors : torch.Tensor or List[torch.Tensor]
        List of anchor boxes of shape `(lx, ly, #anchors, 4)`.
    pos_thresh : float
        IOU threshold of positive anchors.
    neg_thresh : float
        If provided, only non-positive anchors whose ious with all ground truth boxes are
        lower than neg_thresh will be considered negative. Other non-positive anchors will be ignored.
    """

    def __init__(self, anchors, pos_thresh=0.5, neg_thresh=None, debug=False):
        self.a_xywh = flatten(anchors)
        self.a_ltrb = BBox.convert(self.a_xywh, BBox.XYWH, BBox.LTRB)
        self.pos_thresh = pos_thresh
        self.neg_thresh = neg_thresh
        self.get_label = lambda x: 1
        self.debug = debug

    def __call__(self, image_gts):
        is_cpu = self.a_xywh.device.type == 'cpu'
        match_func = match_anchors if is_cpu else match_anchors2
        batch_size = len(image_gts)
        loc_targets = []
        cls_targets = []
        if self.neg_thresh:
            negs = []
        for i in range(batch_size):
            target = match_func(
                image_gts[i], self.a_xywh[i], self.a_ltrb[i],
                self.pos_thresh, self.neg_thresh, self.get_label, self.debug)
            loc_t, cls_t = target[:2]
            loc_targets.append(loc_t)
            cls_targets.append(cls_t)
            if self.neg_thresh:
                neg = target[2]
                negs.append(neg)
        loc_t = torch.cat(loc_targets, dim=0)
        cls_t = torch.cat(cls_targets, dim=0)
        neg = torch.cat(negs, dim=0)

        targets = [loc_t, cls_t]
        if self.neg_thresh:
            targets.append(neg)
        return targets


class InferenceRoIs:

    def __init__(self, anchors, conf_threshold=0.01,
                 iou_threshold=0.5, topk=100,
                 conf_strategy='softmax', nms='soft_nms', soft_nms_threshold=None):
        self.anchors = flatten(anchors)
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.topk = topk
        assert conf_strategy in [
            'softmax', 'sigmoid'], "conf_strategy must be softmax or sigmoid"
        self.conf_strategy = conf_strategy
        self.nms = nms
        self.soft_nms_threshold = soft_nms_threshold

    def __call__(self, loc_p, cls_p):
        image_dets = []
        batch_size = loc_p.size(0)
        for i in range(batch_size):
            dets = inference_rois(
                loc_p[i], cls_p[i], self.anchors,
                self.conf_threshold, self.iou_threshold, self.topk, self.conf_strategy)
            image_dets.append(dets)
        return image_dets


class MatchRoIs:
    def __init__(self, pos_thresh=0.5, n_samples=None, pos_neg_ratio=1/3):
        super().__init__()
        self.pos_thresh = pos_thresh
        self.n_samples = n_samples
        self.pos_neg_ratio = pos_neg_ratio

    def __call__(self, rois, image_gts):
        is_cpu = rois.device.type == 'cpu'
        match_func = match_rois if is_cpu else match_rois2
        batch_size = len(rois)
        rois_ltrb = rois[..., 1:]
        loc_targets = []
        cls_targets = []
        sampled_rois = []
        for i in range(batch_size):
            loc_t, cls_t, indices = match_func(rois_ltrb[i], image_gts[i], None, self.pos_thresh, self.n_samples, self.pos_neg_ratio)
            loc_targets.append(loc_t)
            cls_targets.append(cls_t)
            sampled_rois.append(rois[i][indices])
        loc_t = torch.cat(loc_targets, dim=0)
        cls_t = torch.cat(cls_targets, dim=0)
        rois = torch.cat(sampled_rois, dim=0)

        return loc_t, cls_t, rois



class RCNNLoss(nn.Module):

    def __init__(self, p=0.01):
        super().__init__()
        self.rpn_loss = MultiBoxLoss(p=p, criterion='focal', prefix='RPN')
        self.rcnn_loss = MultiBoxLoss(p=p, prefix='RCNN')

    @property
    def p(self):
        return self.rcnn_loss.p

    @p.setter
    def p(self, new_p):
        self.rpn_loss.p = new_p
        self.rcnn_loss.p = new_p

    def forward(self, loc_p, cls_p, loc_t, cls_t, rpn_loc_p, rpn_cls_p, rpn_loc_t, rpn_cls_t, ignore):
        rpn_loss = self.rpn_loss(rpn_loc_p, rpn_cls_p, rpn_loc_t, rpn_cls_t, ignore)
        rcnn_loss = self.rcnn_loss(loc_p, cls_p, loc_t, cls_t)
        loss = rpn_loss + rcnn_loss
        return loss


@curry
def roi_based_inference(
        rois, loc_p, cls_p, conf_threshold=0.01,
        iou_threshold=0.5, topk=100, nms_method='soft_nms'):
    dets = []
    bboxes = loc_p
    logits = cls_p
    scores = torch.softmax(logits, dim=1)[:, 1:]
    scores, labels = torch.max(scores, dim=1)

    if conf_threshold > 0:
        pos = scores > conf_threshold
        scores = scores[pos]
        labels = labels[pos]
        bboxes = bboxes[pos]
        rois = rois[pos]

    bboxes = target_to_coords(bboxes, rois)

    bboxes = BBox.convert(
        bboxes, format=BBox.XYWH, to=BBox.LTRB, inplace=True).cpu()
    scores = scores.cpu()

    if nms_method == 'nms':
        indices = nms(bboxes, scores, iou_threshold)
        scores = scores[indices]
        labels = labels[indices]
        bboxes = bboxes[indices]
        if scores.size(0) > topk:
            indices = scores.topk(topk)[1]
        else:
            indices = range(scores.size(0))
    else:
        indices = soft_nms_cpu(
            bboxes, scores, iou_threshold, topk, min_score=conf_threshold)
    bboxes = BBox.convert(
        bboxes, format=BBox.LTRB, to=BBox.LTWH, inplace=True)
    for ind in indices:
        det = {
            'image_id': -1,
            'category_id': labels[ind].item() + 1,
            'bbox': bboxes[ind].tolist(),
            'score': scores[ind].item(),
        }
        dets.append(det)
    return dets


class RoIBasedInference:

    def __init__(self, conf_threshold=0.01, iou_threshold=0.5, topk=100, nms_method='soft_nms'):
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.topk = topk
        self.nms_method = nms_method

    def __call__(self, rois, loc_p, cls_p):
        image_dets = []
        batch_size, num_rois = rois.size()[:2]
        rois = BBox.convert(rois, BBox.LTRB, BBox.XYWH, inplace=True)
        loc_p = loc_p.view(batch_size, num_rois, -1)
        cls_p = cls_p.view(batch_size, num_rois, -1)
        for i in range(batch_size):
            dets = roi_based_inference(
                rois[i], loc_p[i], cls_p[i],
                self.conf_threshold, self.iou_threshold, self.topk, self.nms_method)
            image_dets.append(dets)
        return image_dets


@curry
def inference_rois(loc_p, cls_p, anchors, iou_threshold=0.5, topk=100, conf_strategy='softmax'):
    if conf_strategy == 'softmax':
        scores = torch.softmax(cls_p, dim=1)
    else:
        scores = torch.sigmoid_(cls_p)
    scores = scores[:, 1:]
    scores = torch.max(scores, dim=1)[0]

    loc_p[..., :2].mul_(anchors[:, 2:]).add_(anchors[:, :2])
    loc_p[..., 2:].exp_().mul_(anchors[:, 2:])

    bboxes = BBox.convert(
        loc_p, format=BBox.XYWH, to=BBox.LTRB, inplace=True)

    rois = []
    for i in range(len(loc_p)):
        ibboxes = bboxes[i]
        iscores = scores[i]
        indices = nms(ibboxes, iscores, iou_threshold)
        ibboxes = ibboxes[indices]
        iscores = iscores[indices]
        if len(indices) > topk:
            indices = iscores.topk(topk)[1]
            ibboxes = ibboxes[indices]
        else:
            ibboxes = sample(ibboxes, topk)
        batch_idx = ibboxes.new_full((topk, 1), i)
        rois.append(torch.cat([batch_idx, ibboxes], dim=-1))
    rois = torch.stack(rois, dim=0)
    return rois
