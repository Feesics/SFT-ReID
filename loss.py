#! /usr/bin/python
# -*- encoding: utf-8 -*-


import torch
import torch.nn as nn


class LabelSmoothSoftmaxCE(nn.Module):
    def __init__(self,
            lb_pos = 0.9,
            lb_neg = 0.005,
            reduction = 'mean',
            lb_ignore = 255,
            *args, **kwargs):
        super(LabelSmoothSoftmaxCE, self).__init__()
        self.lb_pos = lb_pos
        self.lb_neg = lb_neg
        self.reduction = reduction
        self.lb_ignore = lb_ignore
        self.log_softmax = nn.LogSoftmax(1)

    def forward(self, logits, label):
        logs = self.log_softmax(logits)
        ignore = label.data.cpu()==self.lb_ignore
        label[ignore] = 0
        lb_one_hot = logits.data.clone().zero_().scatter_(1, label.unsqueeze(1), 1)
        label = self.lb_pos * lb_one_hot + self.lb_neg * (1-lb_one_hot)
        ignore = ignore.nonzero()
        N, M = ignore.size()
        a, *b = ignore.chunk(M, dim=1)
        label[[a, torch.arange(label.size(1)), *b]] = 0

        loss = -torch.sum(logs*label, dim=1)
        if self.reduction == 'mean':
            n_samples = loss.view(-1).size()[0]
            loss = torch.sum(loss) / (n_samples-N)
        return loss


class AMSoftmax(nn.Module):
    def __init__(self, in_feats, n_classes=10, m=0.3, s=15, *args, **kwargs):
        super(AMSoftmax, self).__init__()
        self.m = m
        self.s = s
        self.in_feats = in_feats
        self.W = torch.nn.Parameter(torch.randn(in_feats, n_classes), requires_grad=True)
        #  self.ce = nn.CrossEntropyLoss()
        self.ce = LabelSmoothSoftmaxCE(lb_pos=0.9, lb_neg=1.3e-4)
        nn.init.xavier_normal_(self.W, gain=1)

    def forward(self, x, lb):
        assert x.size()[0] == lb.size()[0]
        assert x.size()[1] == self.in_feats
        x_norm = torch.norm(x, p=2, dim=1, keepdim=True).clamp(min=1e-12)
        x_norm = torch.div(x, x_norm)
        w_norm = torch.norm(self.W, p=2, dim=0, keepdim=True).clamp(min=1e-12)
        w_norm = torch.div(self.W, w_norm)
        costh = torch.mm(x_norm, w_norm)
        lb_view = lb.view(-1, 1)
        if lb_view.is_cuda: lb_view = lb_view.cpu()
        delt_costh = torch.zeros(costh.size()).scatter_(1, lb_view, self.m)
        if x.is_cuda: delt_costh = delt_costh.cuda()
        costh_m = costh - delt_costh
        costh_m_s = self.s * costh_m
        loss = self.ce(costh_m_s, lb)
        return loss


class Bottleneck(nn.Module):
    def __init__(self, in_feats, *args, **kwargs):
        super(Bottleneck, self).__init__(*args, **kwargs)
        self.dense = nn.Linear(in_feats, 512)
        self.bn = nn.BatchNorm1d(512)
        self.prelu = nn.PReLU()
        nn.init.kaiming_normal_(self.dense.weight, a=1)
        nn.init.constant_(self.dense.bias, 0)

    def forward(self, x):
        x = self.dense(x)
        x = self.bn(x)
        out = self.prelu(x)
        return out


class BottleneckLoss(nn.Module):
    def __init__(self, in_feats, n_classes=751, *args, **kwargs):
        super(BottleneckLoss, self).__init__(*args, **kwargs)
        self.bottleneck = Bottleneck(in_feats)
        self.am_softmax = AMSoftmax(512, n_classes)

    def forward(self, x, label):
        out = self.bottleneck(x)
        loss = self.am_softmax(out, label)
        return loss


if __name__ == '__main__':
    Loss = AMSoftmax(1024, 10)
    a = torch.randn(20, 1024)
    lb = torch.randint(0, 10, (20, ), dtype = torch.long)
    loss = Loss(a, lb)
    loss.backward()

    Loss_b = BottleneckLoss(1024, 10)
    a = torch.randn(20, 1024)
    lb = torch.randint(0, 10, (20, ), dtype = torch.long)
    loss = Loss_b(a, lb)
    loss.backward()

    print(loss.detach().numpy())
    print(list(Loss.parameters())[0].shape)
    print(type(next(Loss.parameters())))

