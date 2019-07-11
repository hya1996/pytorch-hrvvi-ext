import torch
import torch.nn as nn
import torch.nn.functional as F
from horch.nn import MaxUnpool2d

from torch.nn.utils import spectral_norm

from horch.models.gan.common import Generator


class Discriminator(nn.Module):
    def __init__(self, in_channels, channels=64, q_out_channels=1, size=(32, 32), leaky_slope=0.1, use_sn=True):
        super().__init__()
        self.h, self.w = size
        self.q = True
        self.q_out_channels = q_out_channels
        self.conv = nn.Sequential(
            nn.Conv2d(in_channels, channels, kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(leaky_slope, True),
            nn.Conv2d(channels, channels * 2, kernel_size=4, stride=2, padding=1),
            nn.LeakyReLU(leaky_slope, True),
            nn.Conv2d(channels * 2, channels * 2, kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(leaky_slope, True),
            nn.Conv2d(channels * 2, channels * 4, kernel_size=4, stride=2, padding=1),
            nn.LeakyReLU(leaky_slope, True),
            nn.Conv2d(channels * 4, channels * 4, kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(leaky_slope, True),
            nn.Conv2d(channels * 4, channels * 8, kernel_size=4, stride=2, padding=1),
            nn.LeakyReLU(leaky_slope, True),
            nn.Conv2d(channels * 8, channels * 8, kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(leaky_slope, True),
        )
        self.d_head = nn.Linear((self.h // 8) * (self.w // 8) * channels * 8, 1)

        self.q_head = nn.Sequential(
            nn.Linear((self.h // 8) * (self.w // 8) * channels * 8, channels),
            nn.LeakyReLU(leaky_slope, True),
            nn.Linear(channels, q_out_channels),
        )
        if use_sn:
            for m in self.conv:
                if isinstance(m, nn.Conv2d):
                    spectral_norm(m)
            spectral_norm(self.d_head)
            for m in self.q_head:
                if isinstance(m, nn.Linear):
                    spectral_norm(m)

    def forward(self, x):
        x = self.conv(x)
        x = x.view(x.size(0), -1)
        d_pred = self.d_head(x)
        if not self.q:
            return d_pred
        q_pred = self.q_head(x)
        return d_pred, q_pred


class ResBlock(nn.Module):
    def __init__(self, in_channels, out_channels, scale=None, use_sn=False, use_bn=True):
        assert scale in ['down', 'up', None]
        super().__init__()
        shortcut = nn.Sequential()
        if scale == 'up':
            # shortcut.scale = MaxUnpool2d(True)
            shortcut.scale = nn.UpsamplingNearest2d(scale_factor=2)
        shortcut.conv = nn.Conv2d(in_channels, out_channels, kernel_size=1)
        if scale == 'down':
            shortcut.scale = nn.AvgPool2d(2, 2)
        self.shortcut = shortcut

        conv = nn.Sequential()
        if use_bn:
            conv.bn1 = nn.BatchNorm2d(in_channels)
        conv.relu1 = nn.ReLU(use_bn)

        if scale == 'up':
            conv.scale = nn.UpsamplingNearest2d(scale_factor=2)
        conv.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1)
        if use_bn:
            conv.bn2 = nn.BatchNorm2d(out_channels)
        conv.relu2 = nn.ReLU(use_bn)
        conv.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1)
        if scale == 'down':
            conv.scale = nn.AvgPool2d(2, 2)
        self.conv = conv

        if use_sn:
            spectral_norm(self.shortcut.conv)
            spectral_norm(self.conv.conv1)
            spectral_norm(self.conv.conv2)

    def forward(self, x):
        return self.conv(x) + self.shortcut(x)
