import torch
import torch.nn as nn
import numpy as np
import math
import torch.nn.functional as F
import torchvision.transforms as T

from networks.baselines import *
from utils.inpaint_utils import flow_to_image

def extract_image_patches(img, kernel, stride=1, dilation=1):
    b,c,h,w = img.shape
    h2 = math.ceil(h /stride)
    w2 = math.ceil(w /stride)
    pad_row = (h2 - 1) * stride + (kernel - 1) * dilation + 1 - h
    pad_col = (w2 - 1) * stride + (kernel - 1) * dilation + 1 - w
    # print(h, h2, pad_row)
    # print(w, w2, pad_col)

    x = F.pad(img, (pad_row // 2, pad_row - pad_row // 2, pad_col // 2, pad_col - pad_col // 2))
    patches = x.unfold(2, kernel, stride).unfold(3, kernel, stride)
    patches = patches.permute(0,4,5,1,2,3).contiguous()
    return patches.view(b, -1, patches.shape[-2], patches.shape[-1])

class GateConv2d(nn.Conv2d):
    def __init__(self, activation=None, *args, **kwargs):
        super(GateConv2d, self).__init__(*args, **kwargs)
        self.activation = activation

    def forward(self, x):
        x = F.conv2d(x, self.weight, self.bias, self.stride, self.padding, self.dilation, self.groups)
        if self.out_channels == 3 or self.activation is None:
            return x
        x, y = torch.split(x, x.shape[1] // 2, dim=1)
        x = self.activation(x)
        y = torch.sigmoid(y)
        x = x * y
        return x

class GateTransposed2d(nn.ConvTranspose2d):
    def __init__(self, activation=None, *args, **kwargs):
        super(GateTransposed2d, self).__init__(*args, **kwargs)
        self.activation = activation

    def forward(self, x):
        x = F.conv_transpose2d(x, self.weight, self.bias, self.stride, self.padding, self.dilation, self.groups)
        if self.out_channels == 3 or self.activation is None:
            return x
        x, y = torch.split(x, x.shape[1] // 2, dim=1)
        x = self.activation(x)
        y = torch.sigmoid(y)
        x = x * y
        return x
    
    
class ContextAttention(nn.Module):
    def __init__(self, ksize=3, stride=1, rate=1, fuse_k=3, softmax_scale=10., trainging=True, fuse=True):
        super(ContextAttention, self).__init__()
        self.stride = stride
        self.softmax_scale = softmax_scale
        self.ksize = ksize
        self.rate = rate
        self.fuse_k = fuse_k
        self.fuse = fuse
    
    def forward(self, f, b, mask=None):
        kernel = self.rate * 2
        rate = self.rate
        
        # print(f.shape, b.shape)
        bs, cs, hs, ws = f.shape
        raw_w = extract_image_patches(b, kernel=kernel, stride=self.rate*self.stride, dilation=1)
        # print(raw_w.shape)
        raw_w = torch.reshape(raw_w, (bs, -1, kernel, kernel, cs))
        raw_w = raw_w.permute(0,1,4,2,3)

        f = F.interpolate(f, scale_factor=1./rate)
        fs = f.shape
        b = F.interpolate(b, size=(int(hs/rate), int(ws/rate)))
        # print(mask.shape)
        if mask is not None:
            mask = F.interpolate(mask, size=(int(hs/rate), int(ws/rate)))
        # print(mask.shape)
        # int_fs = f.shape
        ibs, ics, ihs, iws = b.shape
        f_groups = torch.chunk(f, fs[0], dim=0)
        w = extract_image_patches(b, kernel=self.ksize, stride=self.stride, dilation=1)
        # print(f_groups[0].shape)
        # exit(-1)
        w = torch.reshape(w, (bs, -1, self.ksize, self.ksize, cs))
        w = w.permute(0,1,4,2,3)
        # print(w.shape)

        if mask is None:
            mask = torch.zeros((1, 1, ihs, iws))
        
        m = extract_image_patches(mask, kernel=self.ksize, stride=self.stride)
        # print(m.shape)
        m = torch.reshape(m, (bs, -1, 1, self.ksize, self.ksize))
        m = m.permute(0,2,3,4,1)
        # print(m.shape)
        # exit(-1)
        # m = m[0]
        # print(mask.shape)
        mms = (torch.mean(1 - m, dim=(1,2,3)) < 0.85).float().to(f.device)

        w_groups = torch.chunk(w, bs, dim=0)
        raw_w_groups = torch.chunk(raw_w, bs, dim=0)
        mm_groups = torch.chunk(mms, bs, dim=0)
        y = []
        offsets = []
        k = self.fuse_k
        scale = self.softmax_scale
        fuse_weight = torch.reshape(torch.eye(k), [1,1,k,k]).to(f.device)
        # print(f_groups[0].shape)
        for xi, wi, raw_wi, mm in zip(f_groups, w_groups, raw_w_groups, mm_groups):
            wi = wi[0]
            # print(wi.shape)
            # print(torch.isnan(torch.sqrt(torch.sum(wi * wi, (1,2,3)))).int().sum())
            # exit(-1)
            wi_normed = wi / torch.reshape(torch.clamp(torch.sqrt(torch.sum(wi * wi, (1,2,3)) + 1e-8), min=1e-4), [-1, 1, 1, 1])
            # print(torch.isnan(wi_normed).int().sum())
            # print(torch.clamp(torch.sqrt(torch.sum(wi * wi, (1,2,3))), max=1e-4))
            # print(xi.shape, wi_normed.shape)
            yi = F.conv2d(xi, wi_normed, padding=1)
            
            # print(yi.shape)
            if self.fuse:
                yi = torch.reshape(yi, [1, 1, fs[2]*fs[3], ihs*iws])
                yi = F.conv2d(yi, fuse_weight, padding=1)
                yi = torch.reshape(yi, [1, fs[2], fs[3], ihs, iws])
                yi = yi.permute(0,2,1,4,3)
                yi = torch.reshape(yi, [1, 1, fs[2]*fs[3], ihs*iws])
                yi = F.conv2d(yi, fuse_weight, padding=1)
                yi = torch.reshape(yi, [1, fs[3], fs[2], iws, ihs])
                yi = yi.permute(0,2,1,4,3)
            yi = torch.reshape(yi, [1, fs[2], fs[3], iws*ihs])
            
            # softmax to match
            yi = yi * mm
            # print(yi)
            yi = torch.softmax(yi.clone()*scale, 3)
            yi = yi * mm
            
            offset = torch.argmax(yi, 3).int()
            offset = torch.stack([offset // hs, offset % hs], axis=-1)
            offset = offset.permute(0,3,1,2)

            # paste center
            # wi_center = raw_wi[0]
            wi_center = raw_wi[0]
            yi = yi.permute(0,3,1,2)
            # print(yi.shape, wi_center.shape)
            yi = F.conv_transpose2d(yi, wi_center, stride=rate, padding=1) / 4.
            # print(yi.shape)
            # exit(-1)
            y.append(yi)
            offsets.append(offset)

        y = torch.cat(y, 0)
        offsets = torch.cat(offsets, 0)
        h_add = torch.reshape(torch.arange(ihs), [1, 1, ihs, 1]).repeat([bs, 1, 1, iws]).to(f.device)
        w_add = torch.reshape(torch.arange(iws), [1, 1, 1, iws]).repeat([bs, 1, ihs, 1]).to(f.device)
        # print(y.shape, offsets.shape)
        # print(h_add.shape, w_add.shape)
        offsets = offsets - torch.cat([h_add, w_add], 1)
        flows_np = flow_to_image(offsets.data.cpu().numpy())
        flows = torch.from_numpy(flows_np).to(y.device)
        # print(flows.shape)
        if rate != 1:
            flows = F.interpolate(flows, scale_factor=rate)
        
        # exit(-1)
        return y, flows

class GatedCoarse2FineModel(nn.Module):
    def __init__(self, hidden_channels=24):
        super(GatedCoarse2FineModel, self).__init__()
        self.gen_relu = nn.ELU(inplace=True)
        self.hidden_channels = hidden_channels
        self.relu = nn.ReLU(inplace=True)
        self.build_inpaint_model()

    def build_inpaint_model(self):
        self.conv1s = []
        self.bn1s = []
        self.conv1s.append(GateConv2d(self.gen_relu, 3, self.hidden_channels * 2, 3, 2, padding=1))
        self.bn1s.append(nn.InstanceNorm2d(self.hidden_channels))
        self.conv1s.append(GateConv2d(self.gen_relu, self.hidden_channels, self.hidden_channels * 4, 3, 2, padding=1))
        self.bn1s.append(nn.InstanceNorm2d(self.hidden_channels * 2))
        self.conv1s = nn.ModuleList(self.conv1s)
        self.bn1s = nn.ModuleList(self.bn1s)
        self.conv2s = []
        self.bn2s = []
        self.conv2s.append(GateConv2d(self.gen_relu, 3, self.hidden_channels * 2, 3, 2, padding=1))
        self.bn2s.append(nn.InstanceNorm2d(self.hidden_channels))
        self.conv2s.append(GateConv2d(self.relu, self.hidden_channels, self.hidden_channels * 4, 3, 2, padding=1))
        self.bn2s.append(nn.InstanceNorm2d(self.hidden_channels * 2))
        self.conv2s.append(ContextAttention(ksize=3, stride=1, rate=2))
        self.conv2s = nn.ModuleList(self.conv2s)
        self.bn2s = nn.ModuleList(self.bn2s)
        self.total_conv = []
        self.total_bn = []
        self.total_conv.append(GateConv2d(self.gen_relu, self.hidden_channels * 4, self.hidden_channels * 4, 3, 1, padding=1))
        self.total_bn.append(nn.InstanceNorm2d(self.hidden_channels * 2))
        self.total_conv.append(GateTransposed2d(self.gen_relu, self.hidden_channels * 2, self.hidden_channels * 2, 3, 2, padding=1))
        self.total_bn.append(nn.InstanceNorm2d(self.hidden_channels))
        self.total_conv.append(GateTransposed2d(self.gen_relu, self.hidden_channels, self.hidden_channels, 3, 2, padding=1))
        self.total_bn.append(nn.InstanceNorm2d(self.hidden_channels // 2))
        self.total_conv.append(GateConv2d(None, self.hidden_channels // 2, 3, 3, 1, padding=1))
        self.total_conv = nn.ModuleList(self.total_conv)
        self.total_bn = nn.ModuleList(self.total_bn)
    
    def forward(self, x, xori, mask=None):
        x1 = x * mask.repeat(1,3,1,1) + xori * (1. - mask.repeat(1,3,1,1))
        xnow = x1
        for i, conv in enumerate(self.conv1s):
            # print(x1.shape)
            x1 = conv(x1)
            x1 = self.bn1s[i](x1)
            # x1 = self.gen_relu(x1)
            # print(x1.shape)
        x2 = xnow
        offsets = None
        mask_s = F.interpolate(mask, size=(x1.shape[2], x1.shape[3]))
        for i, conv in enumerate(self.conv2s):
            if i == len(self.conv2s) - 1:
                x2, offsets = conv(x2, x2, mask=mask_s)
            else:
                x2 = conv(x2)
                x2 = self.bn2s[i](x2)
            # x2 = self.gen_relu(x2) if i != 1 else self.relu(x2)
        x = torch.cat([x1, x2], 1)
        for i, conv in enumerate(self.total_conv):
            x = conv(x)
            if i < len(self.total_conv) - 1:
                x = self.total_bn[i](x)
                # print(x.shape)
                # x = self.gen_relu(x)
        x = torch.tanh(x)
        return x, offsets

class TinyCoarse2FineModel(nn.Module):
    def __init__(self, hidden_channels=48):
        super(TinyCoarse2FineModel, self).__init__()
        self.gen_relu = nn.ELU(inplace=True)
        self.hidden_channels = hidden_channels
        self.relu = nn.ReLU(inplace=True)
        self.build_inpaint_model()
    
    def build_inpaint_model(self):
        self.conv1s = []
        self.bn1s = []
        self.conv1s.append(nn.Conv2d(3, self.hidden_channels, 3, 2, padding=1))
        self.bn1s.append(nn.InstanceNorm2d(self.hidden_channels))
        self.conv1s.append(nn.Conv2d(self.hidden_channels, self.hidden_channels * 2, 3, 2, padding=1))
        self.bn1s.append(nn.InstanceNorm2d(self.hidden_channels * 2))
        self.conv1s = nn.ModuleList(self.conv1s)
        self.bn1s = nn.ModuleList(self.bn1s)
        self.conv2s = []
        self.bn2s = []
        self.conv2s.append(nn.Conv2d(3, self.hidden_channels, 3, 2, padding=1))
        self.bn2s.append(nn.InstanceNorm2d(self.hidden_channels))
        self.conv2s.append(nn.Conv2d(self.hidden_channels, self.hidden_channels * 2, 3, 2, padding=1))
        self.bn2s.append(nn.InstanceNorm2d(self.hidden_channels * 2))
        self.conv2s.append(ContextAttention(ksize=3, stride=1, rate=2))
        self.conv2s = nn.ModuleList(self.conv2s)
        self.bn2s = nn.ModuleList(self.bn2s)
        self.total_conv = []
        self.total_bn = []
        self.total_conv.append(nn.Conv2d(self.hidden_channels * 4, self.hidden_channels * 2, 3, 1, padding=1))
        self.total_bn.append(nn.InstanceNorm2d(self.hidden_channels * 2))
        self.total_conv.append(nn.ConvTranspose2d(self.hidden_channels * 2, self.hidden_channels, 4, 2, padding=1))
        self.total_bn.append(nn.InstanceNorm2d(self.hidden_channels))
        self.total_conv.append(nn.ConvTranspose2d(self.hidden_channels, self.hidden_channels // 2, 4, 2, padding=1))
        self.total_bn.append(nn.InstanceNorm2d(self.hidden_channels // 2))
        self.total_conv.append(nn.Conv2d(self.hidden_channels // 2, 3, 3, 1, padding=1))
        self.total_conv = nn.ModuleList(self.total_conv)
        self.total_bn = nn.ModuleList(self.total_bn)
    
    def forward(self, x, xori, mask=None):
        x1 = x * mask.repeat(1,3,1,1) + xori * (1. - mask.repeat(1,3,1,1))
        xnow = x1
        for i, conv in enumerate(self.conv1s):
            x1 = conv(x1)
            x1 = self.bn1s[i](x1)
            x1 = self.gen_relu(x1)
        x2 = xnow
        offsets = None
        mask_s = F.interpolate(mask, size=(x1.shape[2], x1.shape[3]))
        for i, conv in enumerate(self.conv2s):
            if i == len(self.conv2s) - 1:
                x2, offsets = conv(x2, x2, mask=mask_s)
            else:
                x2 = conv(x2)
                x2 = self.bn2s[i](x2)
            x2 = self.gen_relu(x2) if i != 1 else self.relu(x2)
        x = torch.cat([x1, x2], 1)
        for i, conv in enumerate(self.total_conv):
            x = conv(x)
            if i < len(self.total_conv) - 1:
                x = self.total_bn[i](x)
                x = self.gen_relu(x)
        x = torch.tanh(x)
        return x, offsets


class Coarse2FineModel(nn.Module):
    def __init__(self, hidden_channels=48, dilation_depth=0):
        super(Coarse2FineModel, self).__init__()
        # Stage1 model
        self.hidden_channels = hidden_channels
        self.dilation_depth = dilation_depth
        self.gen_relu = nn.ELU(inplace=True)
        self.relu = nn.ReLU(inplace=True)
        # self.last_act = nn.Tanh()
        self.build_inpaint_model()

    def build_inpaint_model(self):
        # Define Coarse-to-Fine Network
        # Stage 2, conv branch
        self.conv_1s = []
        self.bn1s = []
        self.conv_1s.append(nn.Conv2d(3, self.hidden_channels, 5, 1, padding=1))
        self.bn1s.append(nn.InstanceNorm2d(self.hidden_channels))
        self.conv_1s.append(nn.Conv2d(self.hidden_channels, self.hidden_channels, 3, 2, padding=1))
        self.bn1s.append(nn.InstanceNorm2d(self.hidden_channels))
        self.conv_1s.append(nn.Conv2d(self.hidden_channels, self.hidden_channels*2, 3, 1, padding=1))
        self.bn1s.append(nn.InstanceNorm2d(self.hidden_channels * 2))
        self.conv_1s.append(nn.Conv2d(self.hidden_channels*2, self.hidden_channels*2, 3, 2, padding=1))
        self.bn1s.append(nn.InstanceNorm2d(self.hidden_channels * 2))
        self.conv_1s.append(nn.Conv2d(self.hidden_channels*2, self.hidden_channels*4, 3, 1, padding=1))
        self.bn1s.append(nn.InstanceNorm2d(self.hidden_channels * 4))
        self.conv_1s.append(nn.Conv2d(self.hidden_channels*4, self.hidden_channels*4, 3, 1, padding=1))
        self.bn1s.append(nn.InstanceNorm2d(self.hidden_channels * 4))
        for i in range(self.dilation_depth):
            self.conv_1s.append(nn.Conv2d(self.hidden_channels*4, self.hidden_channels*4, 3, 1, dilation=2 ** (i + 1), padding=2 ** (i + 1)))
            self.bn1s.append(nn.InstanceNorm2d(self.hidden_channels * 4))
        self.conv_1s = nn.ModuleList(self.conv_1s)
        # Stage 2, attention branch
        self.conv_2s = []
        self.bn2s = []
        self.conv_2s.append(nn.Conv2d(3, self.hidden_channels, 5, 1, padding=2))
        self.bn2s.append(nn.InstanceNorm2d(self.hidden_channels))
        self.conv_2s.append(nn.Conv2d(self.hidden_channels, self.hidden_channels, 3, 2, padding=1))
        self.bn2s.append(nn.InstanceNorm2d(self.hidden_channels))
        self.conv_2s.append(nn.Conv2d(self.hidden_channels, 2*self.hidden_channels, 3, 1, padding=1))
        self.bn2s.append(nn.InstanceNorm2d(self.hidden_channels * 2))
        self.conv_2s.append(nn.Conv2d(self.hidden_channels*2, self.hidden_channels*2, 3, 2, padding=1))
        self.bn2s.append(nn.InstanceNorm2d(self.hidden_channels * 2))
        self.conv_2s.append(nn.Conv2d(self.hidden_channels*2, self.hidden_channels*4, 3, 1, padding=1))
        self.bn2s.append(nn.InstanceNorm2d(self.hidden_channels * 4))
        self.conv_2s.append(nn.Conv2d(self.hidden_channels*4, self.hidden_channels*4, 3, 1, padding=1))
        self.bn2s.append(nn.InstanceNorm2d(self.hidden_channels * 4))
        # context attention
        self.conv_2s.append(ContextAttention(ksize=3, stride=1, rate=2))
        self.conv_2s.append(nn.Conv2d(self.hidden_channels*4, self.hidden_channels*4, 3, 1, padding=1))
        self.bn2s.append(nn.InstanceNorm2d(self.hidden_channels * 4))
        self.conv_2s.append(nn.Conv2d(self.hidden_channels*4, self.hidden_channels*4, 3, 1, padding=1))
        self.bn2s.append(nn.InstanceNorm2d(self.hidden_channels * 4))
        self.conv_2s = nn.ModuleList(self.conv_2s)
        # total merged branch
        self.totals = []
        self.total_bns = []
        self.totals.append(nn.Conv2d(self.hidden_channels*8, self.hidden_channels*4, 3, 1, padding=1))
        self.total_bns.append(nn.InstanceNorm2d(self.hidden_channels * 4))
        self.totals.append(nn.Conv2d(self.hidden_channels*4, self.hidden_channels*4, 3, 1, padding=1))
        self.total_bns.append(nn.InstanceNorm2d(self.hidden_channels * 4))
        self.totals.append(nn.ConvTranspose2d(self.hidden_channels*4, self.hidden_channels*2, 4, 2, padding=1))
        self.total_bns.append(nn.InstanceNorm2d(self.hidden_channels * 2))
        self.totals.append(nn.Conv2d(self.hidden_channels*2, self.hidden_channels*2, 3, 1, padding=1))
        self.total_bns.append(nn.InstanceNorm2d(self.hidden_channels * 2))
        self.totals.append(nn.ConvTranspose2d(self.hidden_channels*2, self.hidden_channels*2, 4, 2, padding=1))
        self.total_bns.append(nn.InstanceNorm2d(self.hidden_channels * 2))
        self.totals.append(nn.Conv2d(self.hidden_channels*2, self.hidden_channels, 3, 1, padding=1))
        self.total_bns.append(nn.InstanceNorm2d(self.hidden_channels))
        self.totals.append(nn.Conv2d(self.hidden_channels, self.hidden_channels // 2, 3, 1, padding=1))
        self.total_bns.append(nn.InstanceNorm2d(self.hidden_channels // 2))
        self.totals.append(nn.Conv2d(self.hidden_channels // 2, 3, 3, 1, padding=1))
        self.totals = nn.ModuleList(self.totals)

    def forward(self, x, xori, mask=None):
        x1 = x * mask.repeat(1,3,1,1) + xori * (1. - mask.repeat(1,3,1,1))
        xnow = x1
        for conv in self.conv_1s:
            x1 = conv(x1)
            x1 = self.gen_relu(x1)
            # print(x1.shape)
        # print(mask.shape)
        # print(x1.shape)
        # x2 = x1 * mask + x * (1. - mask)
        x2 = xnow
        offsets = None
        for i, conv in enumerate(self.conv_2s):
            # print(torch.isnan(x2).int().sum(), i)
            if i == 6:
                # print(x2.shape)
                x2, offsets = conv(x2, x2, mask=mask)
                # print(x2.shape)
            else:
                # print(x2.shape)
                x2 = conv(x2)
                # offsets = None
            x2 = self.gen_relu(x2) if i != 5 else self.relu(x2)
        # print(x1.shape, x2.shape)
        x = torch.cat([x1, x2], 1)
        for i, conv in enumerate(self.totals):
            # if i == 2 or i == 4:
            #     x = F.upsample(x, scale_factor=2)
            # print(x.shape)
            
            x = conv(x)
            if i < len(self.totals) - 1:
                x = self.gen_relu(x)
        x = torch.tanh(x)
        # print(x[0, :, :, 0].mean(), x[0, :, :, 1].mean(), x[0, :, :, 2].mean())
        return x, offsets
