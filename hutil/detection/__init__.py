import random
import numbers
from typing import List

from toolz import curry
from toolz.curried import get, groupby

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data.dataloader import default_collate

from hutil import one_hot
from hutil.common import Args
from hutil.nn.loss import focal_loss2
from hutil import _C

from hutil.detection.bbox import BBox, transform_bbox, transform_bboxes
from hutil.detection.iou import iou_11, iou_b11, iou_1m, iou_mn_cpu

__all__ = [
    "coords_to_target", "MultiLevelAnchorMatching", "BBox",
    "nms_cpu", "soft_nms_cpu", "transform_bbox", "transform_bboxes", "bbox_collate",
    "iou_1m", "iou_11", "iou_b11", "iou_mn_cpu", "draw_bboxes",
    "MultiBoxLoss", "MultiLevelAnchorInference",
    "get_locations", "generate_anchors", "generate_multi_level_anchors",
    "mAP"
]


def get_locations(size, strides):
    num_levels = int(np.log2(strides[-1]))
    lx, ly = size
    locations = [(lx, ly)]
    for _ in range(num_levels):
        # if lx == 3:
        #     lx = 1
        # else:
        #     lx = (lx - 1) // 2 + 1
        # if ly == 3:
        #     ly = 1
        # else:
        #     ly = (ly - 1) // 2 + 1
        lx = (lx - 1) // 2 + 1
        ly = (ly - 1) // 2 + 1
        locations.append((lx, ly))
    return locations[-len(strides):]


def inverse_sigmoid(x, eps=1e-3):
    x = torch.clamp(x, eps, 1 - eps)
    return (x / (1 - x)).log_()


def yolo_coords_to_target(gt_box, anchors, location):
    location = gt_box.new_tensor(location)
    box_txty = inverse_sigmoid(gt_box[:2] * location % 1)
    box_twth = (gt_box[2:] / anchors[..., 2:]).log_()
    return torch.cat((box_txty, box_twth), dim=-1)


def coords_to_target(gt_box, anchors, *args):
    box_txty = (gt_box[:2] - anchors[..., :2]) / anchors[..., 2:]
    box_twth = (gt_box[2:] / anchors[..., 2:]).log_()
    return torch.cat((box_txty, box_twth), dim=-1)


def generate_multi_level_anchors(input_size, strides=(8, 16, 32, 64, 128), aspect_ratios=(1 / 2, 1 / 1, 2 / 1),
                                 scales=(32, 64, 128, 256, 512)):
    width, height = input_size
    locations = get_locations(input_size, strides)
    if isinstance(aspect_ratios[0], numbers.Number):
        aspect_ratios_of_level = [aspect_ratios] * len(strides)
    else:
        aspect_ratios_of_level = aspect_ratios
    aspect_ratios_of_level = torch.tensor(aspect_ratios_of_level)
    anchors_of_level = []
    for (lx, ly), ars, scale in zip(locations, aspect_ratios_of_level, scales):
        if isinstance(scale, tuple):
            sw, sh = scale
        else:
            sw = sh = scale
        anchors = torch.zeros(lx, ly, len(ars), 4)
        anchors[:, :, :, 0] = (torch.arange(
            lx, dtype=torch.float).view(lx, 1, 1).expand(lx, ly, len(ars)) + 0.5) / lx
        anchors[:, :, :, 1] = (torch.arange(
            ly, dtype=torch.float).view(1, ly, 1).expand(lx, ly, len(ars)) + 0.5) / ly
        anchors[:, :, :, 2] = sw * ars.sqrt() / width
        anchors[:, :, :, 3] = sh / ars.sqrt() / height
        anchors_of_level.append(anchors)
    return anchors_of_level


def generate_anchors(input_size, stride=16, aspect_ratios=(1 / 2, 1 / 1, 2 / 1), scales=(32, 64, 128, 256, 512)):
    width, height = input_size
    lx, ly = get_locations(input_size, [stride])[0]
    aspect_ratios = torch.tensor(aspect_ratios)
    scales = aspect_ratios.new_tensor(scales).view(len(scales), -1)
    num_anchors = len(aspect_ratios) * len(scales)
    anchors = torch.zeros(lx, ly, num_anchors, 4)
    anchors[:, :, :, 0] = (torch.arange(
        lx, dtype=torch.float).view(lx, 1, 1).expand(lx, ly, num_anchors) + 0.5) / lx
    anchors[:, :, :, 1] = (torch.arange(
        ly, dtype=torch.float).view(1, ly, 1).expand(lx, ly, num_anchors) + 0.5) / ly
    if scales.size(1) == 2:
        sw = scales[:, [0]]
        sh = scales[:, [1]]
    else:
        sw = sh = scales
    anchors[:, :, :, 2] = (sw * aspect_ratios).view(-1) / width
    anchors[:, :, :, 3] = (sh / aspect_ratios).view(-1) / height
    return anchors


def _ensure_multi_level(xs):
    if torch.is_tensor(xs):
        return [xs]
    else:
        return xs


class MultiLevelAnchorMatching:
    r"""

    Args:
        anchors_of_level: List of anchor boxes of shape `(lx, ly, #anchors, 4)`.
        max_iou: Whether assign anchors with max ious with ground truth boxes as positive anchors.
        pos_thresh: IOU threshold of positive anchors.
        neg_thresh: If provided, only non-positive anchors whose ious with all ground truth boxes are
            lower than neg_thresh will be considered negative. Other non-positive anchors will be ignored.
        get_label: Function to extract label from annotations.
        get_bbox: Function to extract bounding box from annotations. The bounding box must be a sequence
            containing [xmin, ymin, width, height].
        coords_to_target: Function
    Inputs:
        img: Input image.
        anns: Sequences of annotations containing label and bounding box.
    Outputs:
        img: Input image.
        targets:
            loc_targets:
            cls_targets:
            negs (optional): Returned when neg_thresh is provided.
    """

    def __init__(self, anchors_of_level, max_iou=True,
                 pos_thresh=0.5, neg_thresh=None,
                 get_label=get('category_id'),
                 get_bbox=get("bbox"),
                 coords_to_target=coords_to_target,
                 debug=False):
        self.anchors_of_level = _ensure_multi_level(anchors_of_level)
        self.max_iou = max_iou
        self.pos_thresh = pos_thresh
        self.neg_thresh = neg_thresh
        self.get_label = get_label
        self.get_bbox = get_bbox
        self.coords_to_target = coords_to_target
        self.debug = debug

    def __call__(self, img, anns):
        locations = []
        flat_anchors = []
        loc_targets = []
        cls_targets = []
        negs = []
        for anchors in self.anchors_of_level:
            lx, ly = anchors.size()[:2]
            locations.append((lx, ly))
            anchors = anchors.view(-1, 4)
            flat_anchors.append(anchors)
            num_anchors = anchors.size(0)
            loc_targets.append(torch.zeros(num_anchors, 4))
            cls_targets.append(torch.zeros(num_anchors, dtype=torch.long))
            if self.neg_thresh:
                negs.append(torch.ones(num_anchors, dtype=torch.uint8))
            else:
                negs.append(None)

        for ann in anns:
            label = self.get_label(ann)
            bbox = torch.tensor(transform_bbox(
                self.get_bbox(ann), BBox.LTWH, BBox.XYWH))

            max_ious = []
            for anchors, loc_t, cls_t, neg, location in zip(flat_anchors, loc_targets, cls_targets, negs, locations):
                ious = iou_1m(bbox, anchors, format=BBox.XYWH)
                max_ious.append(ious.max(dim=0))

                if self.pos_thresh:
                    pos = ious > self.pos_thresh
                    if pos.any():
                        loc_t[pos] = self.coords_to_target(
                            bbox, anchors[pos], location)
                        cls_t[pos] = label

                if self.neg_thresh:
                    neg &= ious < self.neg_thresh

            f_i, (max_iou, ind) = max(
                enumerate(max_ious), key=lambda x: x[1][0])
            if self.debug:
                print("Feature map %d: %f" % (f_i, max_iou))
                print("BBox   %s" % bbox.tolist())
                print("Anchor %s" % flat_anchors[f_i][ind].tolist())
            loc_targets[f_i][ind] = self.coords_to_target(
                bbox, flat_anchors[f_i][ind], locations[f_i])
            cls_targets[f_i][ind] = label

        if self.neg_thresh:
            ignores = [~neg for neg in negs]
        if len(flat_anchors) == 1:
            loc_targets = loc_targets[0]
            cls_targets = cls_targets[0]
            if self.neg_thresh:
                ignores = ignores[0]

        targets = [loc_targets, cls_targets]
        if self.neg_thresh:
            targets.append(ignores)
        return img, targets


class MultiBoxLoss(nn.Module):

    def __init__(self, neg_pos_ratio=None, p=0.1, criterion='softmax'):
        super().__init__()
        self.neg_pos_ratio = neg_pos_ratio
        self.p = p
        if criterion == 'softmax':
            self.criterion = F.cross_entropy
        elif criterion == 'focal':
            self.criterion = focal_loss2
        else:
            raise ValueError("criterion must be one of softmax or focal")

    def forward(self, loc_preds, cls_preds, loc_targets, cls_targets, ignores=None, *args):
        loc_loss = 0  # loc_preds[0].new_tensor(0., requires_grad=True)
        cls_loss = 0  # loc_preds[0].new_tensor(0., requires_grad=True)
        loc_preds = _ensure_multi_level(loc_preds)
        cls_preds = _ensure_multi_level(cls_preds)
        loc_targets = _ensure_multi_level(loc_targets)
        cls_targets = _ensure_multi_level(cls_targets)
        if ignores is None:
            ignores = [None] * len(loc_preds)
        for loc_p, cls_p, loc_t, cls_t, ignore in zip(loc_preds, cls_preds, loc_targets, cls_targets, ignores):
            pos = cls_t != 0
            num_pos = pos.sum().item()
            if num_pos == 0:
                continue

            if loc_p.size()[:-1] != pos.size():
                loc_loss += F.smooth_l1_loss(
                    loc_p, loc_t, reduction='sum') / num_pos
            else:
                loc_loss += F.smooth_l1_loss(
                    loc_p[pos], loc_t[pos], reduction='sum') / num_pos

            # Hard Negative Mining
            if self.neg_pos_ratio:
                cls_loss_pos = self.criterion(
                    cls_p[pos], cls_t[pos], reduction='sum')

                mask = ~pos
                if ignore is not None:
                    mask = mask & (~ignore)
                cls_p_neg = cls_p[mask]
                cls_loss_neg = -F.log_softmax(cls_p_neg, dim=1)[..., 0]
                num_neg = min(self.neg_pos_ratio * num_pos, len(cls_loss_neg))
                cls_loss_neg = torch.topk(cls_loss_neg, num_neg)[0].sum()
                # print("pos: %.4f | neg: %.4f" % (cls_loss_pos.item() / num_pos, cls_loss_neg.item() / num_pos))
                cls_loss += (cls_loss_pos + cls_loss_neg) / num_pos
            elif ignore is not None:
                if self.criterion == focal_loss2:
                    cls_t = one_hot(cls_t, C=cls_p.size(-1))
                cls_loss_pos = self.criterion(
                    cls_p[pos], cls_t[pos], reduction='sum')

                mask = ~pos & (~ignore)
                cls_loss_neg = self.criterion(
                    cls_p[mask], cls_t[mask], reduction='sum')
                cls_loss += (cls_loss_pos + cls_loss_neg) / num_pos
            else:
                if self.criterion == F.softmax:
                    cls_p = cls_p.transpose(1, -1)
                elif self.criterion == focal_loss2:
                    cls_t = one_hot(cls_t, C=cls_p.size(-1))
                cls_loss += self.criterion(cls_p, cls_t,
                                           reduction='sum') / num_pos
        loss = cls_loss + loc_loss
        if random.random() < self.p:
            print("loc: %.4f | cls: %.4f" %
                  (loc_loss.item(), cls_loss.item()))
        return loss


class MultiLevelAnchorInference:

    def __init__(self, size, anchors_of_level,
                 conf_threshold=0.01,
                 topk_per_level=300, topk=100,
                 iou_threshold=0.5, conf_strategy='softmax', nms='soft_nms'):
        self.width, self.height = size
        self.anchors_of_level = _ensure_multi_level(anchors_of_level)
        self.conf_threshold = conf_threshold
        self.topk_per_level = topk_per_level
        self.topk = topk
        self.iou_threshold = iou_threshold
        assert conf_strategy in [
            'softmax', 'sigmoid'], "conf_strategy must be softmax or sigmoid"
        self.conf_strategy = conf_strategy
        self.nms = nms

    def __call__(self, loc_preds, cls_preds, *args):
        image_dets = []
        loc_preds = _ensure_multi_level(loc_preds)
        cls_preds = _ensure_multi_level(cls_preds)
        num_levels = len(loc_preds)
        batch_size = loc_preds[0].size(0)
        for i in range(batch_size):
            dets = []
            boxes = []
            confs = []
            labels = []
            for loc_p, cls_p, anchors in zip(loc_preds, cls_preds, self.anchors_of_level):
                box = loc_p[i]
                cls_p = cls_p[i]
                anchors = anchors.view(-1, 4)

                if self.conf_strategy == 'softmax':
                    conf = torch.softmax(cls_p, dim=1)
                else:
                    conf = torch.sigmoid_(cls_p)
                conf = conf[..., 1:]
                conf, label = torch.max(conf, dim=1)

                if self.conf_threshold > 0:
                    mask = conf > self.conf_threshold
                    conf = conf[mask]
                    label = label[mask]
                    box = box[mask]
                    anchors = anchors[mask]

                box[:, :2].mul_(anchors[:, 2:]).add_(anchors[:, :2])
                box[:, 2:].exp_().mul_(anchors[:, 2:])
                box[:, [0, 2]] *= self.width
                box[:, [1, 3]] *= self.height

                if num_levels > 1 and len(conf) > self.topk_per_level:
                    conf, indices = conf.topk(self.topk_per_level)
                    box = box[indices]
                    label = label[indices]

                boxes.append(box)
                confs.append(conf)
                labels.append(label)

            if num_levels > 1:
                boxes = torch.cat(boxes, dim=0)
                confs = torch.cat(confs, dim=0)
                labels = torch.cat(labels, dim=0)
            else:
                boxes = boxes[0]
                confs = confs[0]
                labels = labels[0]

            boxes = transform_bboxes(
                boxes, format=BBox.XYWH, to=BBox.LTRB, inplace=True).cpu()
            confs = confs.cpu()

            if self.nms == 'nms':
                indices = nms_cpu(boxes, confs, self.iou_threshold)
            else:
                indices = soft_nms_cpu(
                    boxes, confs, self.iou_threshold, self.topk, conf_threshold=self.conf_threshold / 100)
            boxes = transform_bboxes(
                boxes, format=BBox.LTRB, to=BBox.LTWH, inplace=True)
            for ind in indices:
                det = {
                    'image_id': i,
                    'category_id': labels[ind].item() + 1,
                    'bbox': boxes[ind].tolist(),
                    'score': confs[ind].item(),
                    'scale_w': self.width,
                    'scale_h': self.height,
                }
                dets.append(det)
            image_dets.append(dets)
        return image_dets


def nms_cpu(boxes, confidences, iou_threshold=0.5):
    r"""
    Args:
        boxes (tensor of shape `(N, 4)`): [xmin, ymin, xmax, ymax]
        confidences: Same length as boxes
        iou_threshold (float): Default value is 0.5
    Returns:
        indices: (N,)
    """
    return _C.nms_cpu(boxes, confidences, iou_threshold)


def soft_nms_cpu(boxes, confidences, iou_threshold=0.5, topk=100, conf_threshold=0.01):
    r"""
    Args:
        boxes (tensor of shape `(N, 4)`): [xmin, ymin, xmax, ymax]
        confidences: Same length as boxes
        iou_threshold (float): Default value is 0.5
        topk (int): Topk to remain
        conf_threshold (float): Filter bboxes whose score is less than it to speed up
    Returns:
        indices:
    """
    topk = min(len(boxes), topk)
    return _C.soft_nms_cpu(boxes, confidences, iou_threshold, topk, conf_threshold)


def mAP(detections: List[BBox], ground_truths: List[BBox], iou_threshold=.5):
    r"""
    Args:
        detections: sequences of BBox with `confidence`
        ground_truths: same size sequences of BBox
        iou_threshold:
    """
    image_dts = groupby(lambda b: b.image_id, detections)
    image_gts = groupby(lambda b: b.image_id, ground_truths)
    image_ids = image_gts.keys()
    maps = []
    for i in image_ids:
        i_dts = image_dts.get(i, [])
        i_gts = image_gts[i]
        class_dts = groupby(lambda b: b.category_id, i_dts)
        class_gts = groupby(lambda b: b.category_id, i_gts)
        classes = class_gts.keys()
        aps = []
        for c in classes:
            if c not in class_dts:
                aps.append(0)
                continue
            aps.append(AP(class_dts[c], class_gts[c], iou_threshold))
        maps.append(np.mean(aps))
    return np.mean(maps)

    # class_detections = groupby(lambda b: b.category_id, detections)
    # class_ground_truths = groupby(lambda b: b.category_id, ground_truths)
    # classes = class_ground_truths.keys()
    # for c in classes:
    #     if c not in class_detections:
    #         ret.append(0)
    #         continue
    #
    #     dects = class_detections[c]
    #     gts = class_ground_truths[c]
    #     n_positive = len(gts)
    #
    #     dects = sorted(dects, key=lambda b: b.score, reverse=True)
    #     TP = np.zeros(len(dects))
    #     FP = np.zeros(len(dects))
    #     seen = {k: np.zeros(n)
    #             for k, n in countby(lambda b: b.image_id, gts).items()}
    #
    #     image_gts = groupby(lambda b: b.image_id, gts)
    #     for i, d in enumerate(dects):
    #         gt = image_gts.get(d.image_id, [])
    #         # iou_max = sys.float_info.min
    #         # for j, g in enumerate(gt):
    #         #     iou = iou_11(d.bbox, g.bbox)
    #         #     if iou > iou_max:
    #         #         iou_max = iou
    #         #         j_max = j
    #         ious = [iou_11(d.bbox, g.bbox) for g in gt]
    #         j_max, iou_max = max(enumerate(ious), key=lambda x: x[1])
    #
    #         if iou_max > iou_threshold:
    #             if not seen[d.image_id][j_max]:
    #                 TP[i] = 1
    #                 seen[d.image_id][j_max] = 1
    #             else:
    #                 FP[i] = 1
    #         else:
    #             FP[i] = 1
    #     acc_FP = np.cumsum(FP)
    #     acc_TP = np.cumsum(TP)
    #     recall = acc_TP / n_positive
    #     precision = np.divide(acc_TP, (acc_FP + acc_TP))
    #     t = average_precision(recall, precision)
    #     ret.append(t[0])
    # return sum(ret) / len(ret)


def AP(dts: List[BBox], gts: List[BBox], iou_threshold):
    TP = np.zeros(len(dts), dtype=np.uint8)
    n_positive = len(gts)
    seen = np.zeros(n_positive)
    for i, dt in enumerate(dts):
        ious = [iou_11(dt.bbox, gt.bbox) for gt in gts]
        j_max, iou_max = max(enumerate(ious), key=lambda x: x[1])
        if iou_max > iou_threshold:
            if not seen[j_max]:
                TP[i] = 1
                seen[j_max] = 1
    FP = 1 - TP
    acc_fp = np.cumsum(FP)
    acc_tp = np.cumsum(TP)
    recall = acc_tp / n_positive
    precision = acc_tp / (acc_fp + acc_tp)
    ap = average_precision(recall, precision)[0]
    return ap


def average_precision(recall, precision):
    mrec = [0, *recall, 1]
    mpre = [0, *precision, 0]
    for i in range(len(mpre) - 1, 0, -1):
        mpre[i - 1] = max(mpre[i - 1], mpre[i])
    ii = []
    for i in range(len(mrec) - 1):
        if mrec[1:][i] != mrec[:-1][i]:
            ii.append(i + 1)
    ap = 0
    for i in ii:
        ap += np.sum((mrec[i] - mrec[i - 1]) * mpre[i])
    return ap, mpre[:-1], mrec[:-1], ii


@curry
def bbox_collate(batch, label_transform=lambda x: x):
    xs, targets = zip(*batch)
    image_gts = []
    for i, anns in enumerate(targets):
        gts = []
        for ann in anns:
            ann = {
                **ann,
                'image_id': i,
            }
            ann['category_id'] = label_transform(ann['category_id'])
            gts.append(ann)
        image_gts.append(gts)
    return default_collate(xs), Args(image_gts)


def draw_bboxes(img, anns, categories=None):
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    fig, ax = plt.subplots(1)
    ax.imshow(img)
    for ann in anns:
        if isinstance(ann, BBox):
            ann = ann.to_ann()
        bbox = ann["bbox"]
        rect = Rectangle(bbox[:2], bbox[2], bbox[3], linewidth=1,
                         edgecolor='r', facecolor='none')
        ax.add_patch(rect)
        if categories:
            ax.text(bbox[0], bbox[1],
                    categories[ann["category_id"]], fontsize=12)
    return fig, ax
