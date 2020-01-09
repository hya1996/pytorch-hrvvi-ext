import torch
import torch.nn as nn
import torch.nn.functional as F

from horch.models.bifpn import BiFPN
from horch.models.modules import Conv2d


class WeightedFusion(nn.Module):
    def __init__(self, n):
        super().__init__()
        self.weight = nn.Parameter(torch.full((n,), 1.0 / n), requires_grad=True)
        self.eps = 1e-4

    def forward(self, *xs):
        n = len(xs)
        assert n == self.weight.size(0)
        w = torch.relu(self.weight)
        w = w / (torch.sum(w, dim=0) + self.eps)
        x = 0
        for i in range(n):
            x += w[i] * xs[i]
        return x


class SideHead(nn.Module):

    def __init__(self, side_in_channels):
        super().__init__()
        n = len(side_in_channels)
        self.sides = nn.ModuleList([
            nn.Sequential(
                Conv2d(c, 1, 1, norm_layer='default')
            )
            for c in side_in_channels
        ])
        self.weight = nn.Parameter(torch.zeros((len(side_in_channels),)), requires_grad=True)

    def forward(self, *cs):
        size = cs[0].size()[2:4]
        w = torch.softmax(self.weight, dim=0)

        p = w[0] * self.sides[0](cs[0])
        for i in range(1, len(cs)):
            c = self.sides[i](cs[i])
            c = F.interpolate(c, size, mode='bilinear', align_corners=False)
            p += w[i] * c
        return p


class EED(nn.Module):
    def __init__(self, backbone, in_channels_list, f_channels=128, num_fpn_layers=2, drop_rate=0.0):
        super().__init__()
        self.backbone = backbone
        self.num_fpn_layers = num_fpn_layers
        n = len(in_channels_list)
        self.num_levels = n
        self.lats = nn.ModuleList([
            Conv2d(c, f_channels, kernel_size=1, norm_layer='default')
            for c in in_channels_list
        ])
        self.fpns = nn.ModuleList([
            BiFPN([f_channels] * n, f_channels)
            for _ in range(num_fpn_layers)
        ])
        self.head = SideHead([f_channels] * n)

        self.weights = nn.Parameter(
            torch.zeros((self.num_fpn_layers + 1, self.num_levels)), requires_grad=True)
        self.dropout = nn.Dropout2d(drop_rate)

    def get_param_groups(self):
        group1 = self.backbone.parameters()
        layers = [
            self.fpn, self.head
        ]
        group2 = [
            p
            for l in layers
            for p in l.parameters()
        ]
        return [group1, group2]

    def forward(self, x):
        p1, p2, p3, _, p5 = self.backbone(x)
        ps = [p1, p2, p3, p5]

        ps = [
            self.dropout(p)
            for p in ps
        ]

        ps = [lat(p) for p, lat in zip(ps, self.lats)]

        ws = torch.softmax(self.weights, dim=1)

        fuses = [ws[0, i] * ps[i] for i in range(self.num_levels)]
        for i, fpn in enumerate(self.fpns):
            ps = fpn(*ps)
            for j in range(self.num_levels):
                fuses[j] += ws[i + 1, j] * ps[j]
        p = self.head(*fuses)
        return p