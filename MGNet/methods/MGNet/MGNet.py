import numpy as np
import timm
import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.checkpoint import checkpoint

from methods.module.base_model import BasicModelClass
from methods.module.conv_block import ConvBNReLU
from utils.builder import MODELS
from utils.ops import cus_sample

import torch
import torch.nn as nn
from utils import recorder, io

class PPG_Atrous(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(PPG_Atrous, self).__init__()

        self.branch1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=6, dilation=6)
        self.branch2 = nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=12, dilation=12)
        self.branch3 = nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=18, dilation=18)
        self.branch4 = nn.Conv2d(in_channels, out_channels, kernel_size=1)
        self.global_pooling = nn.AdaptiveAvgPool2d(192)
        self.branch5 = nn.Conv2d(4*in_channels,out_channels,kernel_size=1)

    def forward(self, x):
        out1 = self.branch1(x)
        out2 = self.branch2(x)
        out3 = self.branch3(x)
        out4 = self.branch4(self.global_pooling(x))

        # Concatenate the outputs along the channel dimension
        out = torch.cat([out1, out2, out3, out4], dim=1)
        out = self.branch5(out)
        return out

class ResidualBlock(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(ResidualBlock, self).__init__()
        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1)
        self.relu = nn.ReLU()
        self.conv2 = nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1)

    def forward(self, x):
        residual = x
        out = self.conv1(x)
        out = self.relu(out)
        out = self.conv2(out)
        out = self.relu(out)
        out += residual
        return out


class PPG_front(nn.Module):
    def __init__(self, in_channels, num_masks):
        super(PPG_front, self).__init__()
        self.conv_block1 = nn.Sequential(
            nn.Conv2d(in_channels + num_masks, 32, kernel_size=3, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 32, kernel_size=3, padding=1),
            nn.ReLU(inplace=True)
        )
        self.res_block1 = ResidualBlock(32, 32)
        self.res_block2 = ResidualBlock(32, 32)

    def forward(self, x, yt_1):
        residual = x
        concatenated = torch.cat((x, yt_1), dim=1)
        out = self.conv_block1(concatenated)
        out = self.res_block1(out)
        out = self.res_block2(out)
        return out+residual

class ASPP(nn.Module):
    def __init__(self, in_dim, out_dim):
        super(ASPP, self).__init__()
        self.conv1 = ConvBNReLU(in_dim, out_dim, kernel_size=1)
        self.conv2 = ConvBNReLU(in_dim, out_dim, kernel_size=3, dilation=2, padding=2)
        self.conv3 = ConvBNReLU(in_dim, out_dim, kernel_size=3, dilation=5, padding=5)
        self.conv4 = ConvBNReLU(in_dim, out_dim, kernel_size=3, dilation=7, padding=7)
        self.conv5 = ConvBNReLU(in_dim, out_dim, kernel_size=1)
        self.fuse = ConvBNReLU(5 * out_dim, out_dim, 3, 1, 1)

    def forward(self, x):
        conv1 = self.conv1(x)
        conv2 = self.conv2(x)
        conv3 = self.conv3(x)
        conv4 = self.conv4(x)
        conv5 = self.conv5(cus_sample(x.mean((2, 3), keepdim=True), mode="size", factors=x.size()[2:]))
        return self.fuse(torch.cat((conv1, conv2, conv3, conv4, conv5), 1))


class TransLayer(nn.Module):
    def __init__(self, out_c):
        super().__init__()

    def forward(self, xs):
        assert isinstance(xs, (tuple, list))
        assert len(xs) == 5
        c1, c2, c3, c4, c5 = xs
        return c5, c4, c3, c2, c1


class FRM(nn.Module):
    def __init__(self, in_dim):
        super().__init__()
        self.conv_l_pre_down = ConvBNReLU(in_dim, in_dim, 5, stride=1, padding=2)
        self.conv_l_post_down = ConvBNReLU(in_dim, in_dim, 3, 1, 1)
        self.conv_m = ConvBNReLU(in_dim, in_dim, 3, 1, 1)
        self.conv_s_pre_up = ConvBNReLU(in_dim, in_dim, 3, 1, 1)
        self.conv_s_post_up = ConvBNReLU(in_dim, in_dim, 3, 1, 1)
        self.trans = nn.Sequential(
            ConvBNReLU(3 * in_dim, in_dim, 1),
            ConvBNReLU(in_dim, in_dim, 3, 1, 1),
            ConvBNReLU(in_dim, in_dim, 3, 1, 1),
            nn.Conv2d(in_dim, 3, 1),
        )


    def forward(self, l, m, s, return_feats=False):
       
        tgt_size = m.shape[2:]
        
        l = self.conv_l_pre_down(l)
        l = F.adaptive_max_pool2d(l, tgt_size) + F.adaptive_avg_pool2d(l, tgt_size)
        l = self.conv_l_post_down(l)
      
        m = self.conv_m(m)
      
        s = self.conv_s_pre_up(s)
        s = cus_sample(s, mode="size", factors=m.shape[2:])
        s = self.conv_s_post_up(s)
        attn = self.trans(torch.cat([l,m,s], dim=1))
        attn_l, attn_m,attn_s= torch.softmax(attn, dim=1).chunk(3, dim=1)
        lms = attn_l * l + attn_m * m +attn_s * s
        return lms

class HCDU(nn.Module):
    def __init__(self, in_c, num_groups=4, hidden_dim=None):
        super().__init__()
        self.num_groups = num_groups

        hidden_dim = hidden_dim or in_c // 2
        expand_dim = hidden_dim * num_groups
        self.expand_conv = ConvBNReLU(in_c, expand_dim, 1)
        self.gate_genator = nn.Sequential(
            nn.AdaptiveAvgPool2d((1, 1)),
            nn.Conv2d(num_groups * hidden_dim, hidden_dim, 1),
            nn.ReLU(True),
            nn.Conv2d(hidden_dim, num_groups * hidden_dim, 1),
            nn.Softmax(dim=1),
        )

        self.interact = nn.ModuleDict()
        self.interact["0"] = ConvBNReLU(hidden_dim, 3 * hidden_dim, 3, 1, 1)
        for group_id in range(1, num_groups - 1):
            self.interact[str(group_id)] = ConvBNReLU(2 * hidden_dim, 3 * hidden_dim, 3, 1, 1)
        self.interact[str(num_groups - 1)] = ConvBNReLU(2 * hidden_dim, 2 * hidden_dim, 3, 1, 1)

        self.fuse = nn.Sequential(nn.Conv2d(num_groups * hidden_dim, in_c, 3, 1, 1), nn.BatchNorm2d(in_c))
        self.final_relu = nn.ReLU(True)

    def forward(self, x):
        xs = self.expand_conv(x).chunk(self.num_groups, dim=1)

        outs = []

        branch_out = self.interact["0"](xs[0])
        outs.append(branch_out.chunk(3, dim=1))

        for group_id in range(1, self.num_groups - 1):
            branch_out = self.interact[str(group_id)](torch.cat([xs[group_id], outs[group_id - 1][1]], dim=1))
            outs.append(branch_out.chunk(3, dim=1))

        group_id = self.num_groups - 1
        branch_out = self.interact[str(group_id)](torch.cat([xs[group_id], outs[group_id - 1][1]], dim=1))
        outs.append(branch_out.chunk(2, dim=1))

        out = torch.cat([o[0] for o in outs], dim=1)
        gate = self.gate_genator(torch.cat([o[-1] for o in outs], dim=1))
        out = self.fuse(out * gate)
        return self.final_relu(out + x)


def get_coef(iter_percentage, method):
    if method == "linear":
        milestones = (0.3, 0.7)
        coef_range = (0, 1)
        min_point, max_point = min(milestones), max(milestones)
        min_coef, max_coef = min(coef_range), max(coef_range)
        if iter_percentage < min_point:
            ual_coef = min_coef
        elif iter_percentage > max_point:
            ual_coef = max_coef
        else:
            ratio = (max_coef - min_coef) / (max_point - min_point)
            ual_coef = ratio * (iter_percentage - min_point)
    elif method == "cos":
        coef_range = (0, 1)
        min_coef, max_coef = min(coef_range), max(coef_range)
        normalized_coef = (1 - np.cos(iter_percentage * np.pi)) / 2
        ual_coef = normalized_coef * (max_coef - min_coef) + min_coef
    else:
        ual_coef = 1.0
    return ual_coef


def cal_ual(seg_logits, seg_gts):
    assert seg_logits.shape == seg_gts.shape, (seg_logits.shape, seg_gts.shape)
    sigmoid_x = seg_logits.sigmoid()
    loss_map = (1 - (2 * sigmoid_x - 1).abs().pow(2))
    return loss_map.mean()


@MODELS.register()
class MGNet(BasicModelClass):
    def __init__(self, last_module=ASPP):
        super().__init__()
        self.shared_encoder = timm.create_model(model_name="resnext101_32x8d", pretrained=True, in_chans=3, features_only=True)
        self.translayer = TransLayer(out_c=64)  # [c5, c4, c3, c2, c1]
        self.merge_layers = nn.ModuleList([FRM(in_dim=in_c) for in_c in (2048, 1024, 512, 256, 64)])

        self.d5 = nn.Sequential(HCDU(64, num_groups=6, hidden_dim=32))
        self.d4 = nn.Sequential(HCDU(64, num_groups=6, hidden_dim=32))
        self.d3 = nn.Sequential(HCDU(64, num_groups=6, hidden_dim=32))
        self.d2 = nn.Sequential(HCDU(64, num_groups=6, hidden_dim=32))
        self.d1 = nn.Sequential(HCDU(64, num_groups=6, hidden_dim=32))
        self.out_layer_00 = ConvBNReLU(64, 32, 3, 1, 1)
        self.out_layer_01 = nn.Conv2d(32, 1, 1)
        self.iter_layer_1 = PPG_front(32, 1)
        self.iter_layer_2 = PPG_Atrous(32, 32)
        self.c5_down = nn.Sequential(
            # ConvBNReLU(2048, 256, 3, 1, 1),
            last_module(in_dim=2048, out_dim=64),
        )
        self.c4_down = nn.Sequential(ConvBNReLU(1024, 64, 3, 1, 1))
        self.c3_down = nn.Sequential(ConvBNReLU(512, 64, 3, 1, 1))
        self.c2_down = nn.Sequential(ConvBNReLU(256, 64, 3, 1, 1))

    def encoder_translayer(self, x):
        en_feats = self.shared_encoder(x)
        trans_feats = self.translayer(en_feats)
        return trans_feats

    def body(self, l_scale, m_scale, s_scale):
        l_trans_feats = self.encoder_translayer(l_scale)
        m_trans_feats = self.encoder_translayer(m_scale)
        s_trans_feats = self.encoder_translayer(s_scale)



        feats = []


        for l, m, s,layer in zip(l_trans_feats, m_trans_feats, s_trans_feats,self.merge_layers):
            siu_outs = layer(l=l, m=m, s=s)
            feats.append(siu_outs)



        x = feats[0]
        x = self.c5_down(x)
        x = self.d5(x)
        x = cus_sample(x, mode="scale", factors=2)
        feats[1] = self.c4_down(feats[1])
        x = self.d4(x + feats[1])
        x = cus_sample(x, mode="scale", factors=2)
        feats[2] = self.c3_down(feats[2])
        x = self.d3(x + feats[2])
        x = cus_sample(x, mode="scale", factors=2)
        feats[3] = self.c2_down(feats[3])
        x = self.d2(x + feats[3])
        x = cus_sample(x, mode="scale", factors=2)
        x = self.d1(x + feats[4])
        x = self.out_layer_00(x)
        logits = self.out_layer_01(x)
        x = self.iter_layer_1(x, logits)
        x = self.iter_layer_2(x)
        logits = self.out_layer_01(x)
        logits = cus_sample(logits, mode="scale", factors=2)
        result_list = [
            {"seg":logits},
        ]
        return result_list






    def train_forward(self, data, **kwargs):
        assert not {"image1.2", "image1.0", "image0.7", "mask"}.difference(set(data)), set(data)

        output = self.body(
            l_scale=data["image1.2"],
            m_scale=data["image1.0"],
            s_scale=data["image0.7"],
        )
        loss, loss_str = self.cal_loss(
            all_preds=output[0],

            gts=data["mask"],

            iter_percentage=kwargs["curr"]["iter_percentage"],
        )
        return dict(sal=output[0]["seg"].sigmoid()), loss, loss_str

    def test_forward(self, data, **kwargs):
        l_scale = data["image1.2"]
        m_scale = data["image1.0"]
        s_scale = data["image0.7"]


        output = self.body(
                     l_scale=l_scale,
                    m_scale=m_scale,
                     s_scale=s_scale,
                 )

        return output[0]["seg"]

    def cal_loss(self, all_preds: dict, gts: torch.Tensor,method="cos", iter_percentage: float = 0):
        ual_coef = get_coef(iter_percentage, method)

        losses = []
        loss_str = []
        # for main
        for name, preds in all_preds.items():
            resized_gts = cus_sample(gts, mode="size", factors=preds.shape[2:])


            sod_loss = F.binary_cross_entropy_with_logits(input=preds, target=resized_gts, reduction="mean")
            losses.append(sod_loss)
            loss_str.append(f"{name}_BCE: {sod_loss.item():.5f}")

            ual_loss = 1.5*cal_ual(seg_logits=preds, seg_gts=resized_gts)
            ual_loss *= ual_coef
            losses.append(ual_loss)
            loss_str.append(f"{name}_UAL_{ual_coef:.5f}: {ual_loss.item():.5f}")
        return sum(losses), " ".join(loss_str)

    def get_grouped_params(self):
        param_groups = {}
        for name, param in self.named_parameters():
            if name.startswith("shared_encoder.layer"):
                param_groups.setdefault("pretrained", []).append(param)
            elif name.startswith("shared_encoder."):
                param_groups.setdefault("fixed", []).append(param)
            else:
                param_groups.setdefault("retrained", []).append(param)
        return param_groups


