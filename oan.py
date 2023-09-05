import torch
import torch.nn as nn
from loss import batch_episym
from Transformer import Transformer
from einops import rearrange, repeat

def knn(x, k):
    inner = -2*torch.matmul(x.transpose(2, 1), x) #inner[32,2000,2000]内积？
    xx = torch.sum(x**2, dim=1, keepdim=True) #xx[32,1,2000]
    pairwise_distance = -xx - inner - xx.transpose(2, 1) #distance[32,2000,2000]****记得回头看

    idx = pairwise_distance.topk(k=k, dim=-1)[1]   # (batch_size, num_points, k) [32,2000,9] [32,1000,6]

    return idx[:, :, :]

def get_graph_feature(x, k=20, idx=None):
    #x[32,128,2000,1],k=9
    # x[32,128,1000,1],k=6
    batch_size = x.size(0)
    num_points = x.size(2)
    x = x.view(batch_size, -1, num_points) #x[32,128,2000]
    if idx is None:
        idx_out = knn(x, k=k) #idx_out[32,2000,9]
    else:
        idx_out = idx
    device = x.device

    idx_base = torch.arange(0, batch_size, device=device).view(-1, 1, 1)*num_points

    idx = idx_out + idx_base #idx[32,2000,9] 把32个批次的标号连续了

    idx = idx.view(-1) #idx[32*2000*9] 把32个批次连在一起了 [32*1000*6]

    _, num_dims, _ = x.size()

    x = x.transpose(2, 1).contiguous() #x[32,2000,128]
    feature = x.view(batch_size*num_points, -1)[idx, :]
    feature = feature.view(batch_size, num_points, k, num_dims) #feature[32,2000,9,128]
    x = x.view(batch_size, num_points, 1, num_dims).repeat(1, 1, k, 1) #x[32,2000,9,128]
    feature = torch.cat((x, x - feature), dim=3).permute(0, 3, 1, 2).contiguous() #feature[32,256,2000,9] 图特征
    return feature

class ResNet_Block(nn.Module):
    def __init__(self, inchannel, outchannel, pre=False):
        super(ResNet_Block, self).__init__()
        self.pre = pre
        self.right = nn.Sequential(
            nn.Conv2d(inchannel, outchannel, (1, 1)),
        )
        self.left = nn.Sequential(
            nn.Conv2d(inchannel, outchannel, (1, 1)),
            nn.InstanceNorm2d(outchannel),
            nn.BatchNorm2d(outchannel),
            nn.ReLU(),
            nn.Conv2d(outchannel, outchannel, (1, 1)),
            nn.InstanceNorm2d(outchannel),
            nn.BatchNorm2d(outchannel),
        )

    def forward(self, x):
        x1 = self.right(x) if self.pre is True else x
        out = self.left(x)
        out = out + x1
        return torch.relu(out)

def batch_symeig(X):
    # it is much faster to run symeig on CPU
    X = X.cpu()
    b, d, _ = X.size()
    bv = X.new(b,d,d)
    for batch_idx in range(X.shape[0]):
        e,v = torch.linalg.eigh(X[batch_idx,:,:].squeeze(), UPLO='U')
        bv[batch_idx,:,:] = v
    bv = bv.cuda()
    return bv

def weighted_8points(x_in, logits):
    # x_in: batch * 1 * N * 4 [32,1,500,4] logits[32,2,500,1]
    mask = logits[:, 0, :, 0] #[32,500] logits的第一层
    weights = logits[:, 1, :, 0] #[32,500] logits的第二层

    mask = torch.sigmoid(mask)
    weights = torch.exp(weights) * mask
    weights = weights / (torch.sum(weights, dim=-1, keepdim=True) + 1e-5)

    x_shp = x_in.shape
    x_in = x_in.squeeze(1)

    xx = torch.reshape(x_in, (x_shp[0], x_shp[2], 4)).permute(0, 2, 1).contiguous()

    X = torch.stack([
        xx[:, 2] * xx[:, 0], xx[:, 2] * xx[:, 1], xx[:, 2],
        xx[:, 3] * xx[:, 0], xx[:, 3] * xx[:, 1], xx[:, 3],
        xx[:, 0], xx[:, 1], torch.ones_like(xx[:, 0])
    ], dim=1).permute(0, 2, 1).contiguous()
    wX = torch.reshape(weights, (x_shp[0], x_shp[2], 1)) * X
    XwX = torch.matmul(X.permute(0, 2, 1).contiguous(), wX)

    # Recover essential matrix from self-adjoing eigen

    v = batch_symeig(XwX)
    e_hat = torch.reshape(v[:, :, 0], (x_shp[0], 9))

    # Make unit norm just in case
    e_hat = e_hat / torch.norm(e_hat, dim=1, keepdim=True)
    return e_hat

class DGCNN_Block(nn.Module):
    def __init__(self, knn_num=9, in_channel=128):
        super(DGCNN_Block, self).__init__()
        self.knn_num = knn_num
        self.in_channel = in_channel

        assert self.knn_num == 9 or self.knn_num == 6
        if self.knn_num == 9:
            self.conv = nn.Sequential(
                nn.Conv2d(self.in_channel*2, self.in_channel, (1, 3), stride=(1, 3)), #[32,128,2000,9]→[32,128,2000,3]
                nn.BatchNorm2d(self.in_channel),
                nn.ReLU(inplace=True),
                nn.Conv2d(self.in_channel, self.in_channel, (1, 3)), #[32,128,2000,3]→[32,128,2000,1]
                nn.BatchNorm2d(self.in_channel),
                nn.ReLU(inplace=True),
            )
        if self.knn_num == 6:
            self.conv = nn.Sequential(
                nn.Conv2d(self.in_channel*2, self.in_channel, (1, 3), stride=(1, 3)), #[32,128,2000,6]→[32,128,2000,2]
                nn.BatchNorm2d(self.in_channel),
                nn.ReLU(inplace=True),
                nn.Conv2d(self.in_channel, self.in_channel, (1, 2)), #[32,128,2000,2]→[32,128,2000,1]
                nn.BatchNorm2d(self.in_channel),
                nn.ReLU(inplace=True),
            )

    def forward(self, features):
        #feature[32,128,2000,1]
        B, _, N, _ = features.shape
        out = get_graph_feature(features, k=self.knn_num)
        out = self.conv(out) #out[32,128,2000,1]
        return out

class GCN_Block(nn.Module):
    def __init__(self, in_channel):
        super(GCN_Block, self).__init__()
        self.in_channel = in_channel
        self.conv = nn.Sequential(
            nn.Conv2d(self.in_channel, self.in_channel, (1, 1)),
            nn.BatchNorm2d(self.in_channel),
            nn.ReLU(inplace=True),
        )

    def attention(self, w):
        w = torch.relu(torch.tanh(w)).unsqueeze(-1) #w[32,2000,1] 变成0到1的权重
        A = torch.bmm(w,w.transpose(1, 2)) #A[32,1,1]
        return A

    def graph_aggregation(self, x, w):
        B, _, N, _ = x.size() #B=32,N=2000
        with torch.no_grad():
            A = self.attention(w) #A[32,1,1]
            I = torch.eye(N).unsqueeze(0).to(x.device).detach() #I[1,2000,2000]单位矩阵
            A = A + I #A[32,2000,2000]
            D_out = torch.sum(A, dim=-1) #D_out[32,2000]
            D = (1 / D_out) ** 0.5
            D = torch.diag_embed(D) #D[32,2000,2000]
            L = torch.bmm(D, A)
            L = torch.bmm(L, D) #L[32,2000,2000]
        out = x.squeeze(-1).transpose(1, 2).contiguous() #out[32,2000,128]
        out = torch.bmm(L, out).unsqueeze(-1)
        out = out.transpose(1, 2).contiguous() #out[32,128,2000,1]

        return out

    def forward(self, x, w):
        #x[32,128,2000,1],w[32,2000]
        out = self.graph_aggregation(x, w)
        out = self.conv(out)
        return out

class DS_Block(nn.Module):
    def __init__(self, initial=False, predict=False, out_channel=128, k_num=8, sampling_rate=0.5):
        super(DS_Block, self).__init__()
        self.initial = initial
        self.in_channel = 4 if self.initial is True else 6
        self.out_channel = out_channel
        self.k_num = k_num
        self.predict = predict
        self.sr = sampling_rate

        self.conv = nn.Sequential(
            nn.Conv2d(self.in_channel, self.out_channel, (1, 1)), #4或6 → 128
            nn.BatchNorm2d(self.out_channel),
            nn.ReLU(inplace=True)
        )

        self.gcn = GCN_Block(self.out_channel)

        self.embed_v0 = nn.Sequential(
            ResNet_Block(self.out_channel, self.out_channel, pre=False),
            ResNet_Block(self.out_channel, self.out_channel, pre=False),
            ResNet_Block(self.out_channel, self.out_channel, pre=False),
            ResNet_Block(self.out_channel, self.out_channel, pre=False),
            DGCNN_Block(self.k_num, self.out_channel),
        )
        self.HAT = HAT(self.out_channel)
        self.embed_v1 = nn.Sequential(
            ResNet_Block(self.out_channel, self.out_channel, pre=False),
            ResNet_Block(self.out_channel, self.out_channel, pre=False),
            ResNet_Block(self.out_channel, self.out_channel, pre=False),
            ResNet_Block(self.out_channel, self.out_channel, pre=False),
        )
        self.cont1 = nn.Sequential(
            nn.Conv2d(self.out_channel*2, self.out_channel,1,1),
            nn.BatchNorm2d(self.out_channel),
            nn.ReLU(inplace=True)
        )
        self.cont2 = nn.Sequential(
            nn.Conv2d(self.out_channel*2, self.out_channel,1,1),
            nn.BatchNorm2d(self.out_channel),
            nn.ReLU(inplace=True)
        )
        self.cont3 = nn.Sequential(
            nn.Conv2d(self.out_channel*2, self.out_channel,1,1),
            nn.BatchNorm2d(self.out_channel),
            nn.ReLU(inplace=True)
        )
        self.embed_1 = nn.Sequential(
            ResNet_Block(self.out_channel, self.out_channel, pre=False),
        )
        self.linear_0 = nn.Conv2d(self.out_channel, 1, (1, 1))
        self.linear_1 = nn.Conv2d(self.out_channel, 1, (1, 1))

        if self.predict == True:
            self.embed_2 = ResNet_Block(self.out_channel, self.out_channel, pre=False)
            self.linear_2 = nn.Conv2d(self.out_channel, 2, (1, 1))

    def down_sampling(self, x, y, weights, indices, features=None, predict=False):
        B, _, N , _ = x.size()
        indices = indices[:, :int(N*self.sr)] #indices[32,1000]剪枝剪掉一半
        with torch.no_grad():
            y_out = torch.gather(y, dim=-1, index=indices) #y_out 剪枝后保留的标签[32,1000]
            w_out = torch.gather(weights, dim=-1, index=indices) #w_out 剪枝后保留的w0[32,1000]
        indices = indices.view(B, 1, -1, 1) #indices[32,1,1000,1]

        if predict == False:
            with torch.no_grad():
                x_out = torch.gather(x[:, :, :, :4], dim=2, index=indices.repeat(1, 1, 1, 4)) #x_out 剪枝后保留的x[32,1,1000,4]
            return x_out, y_out, w_out
        else:
            with torch.no_grad():
                x_out = torch.gather(x[:, :, :, :4], dim=2, index=indices.repeat(1, 1, 1, 4)) #x_out 剪枝后保留的x[32,1,500,4]
            feature_out = torch.gather(features, dim=2, index=indices.repeat(1, 128, 1, 1)) #feature_out 剪枝后保留的features[32,128,500,1]
            return x_out, y_out, w_out, feature_out

    def forward(self, x, y):
        # x[32,1,2000,4],y[32,2000]
        # x_[32,1,1000,6],y1[32,1000]
        B, _, N , _ = x.size()
        out = x.transpose(1, 3).contiguous() #contiguous断开out与x的依赖关系。out[32,4或6,2000,1]
        out = self.conv(out) #out[32,128,2000,1]

        # out = self.embed_0(out) #out[32,128,2000,1] [32,128,1000,1]
        out_0 = self.embed_v0(out)
        out_hat1 = self.HAT(out_0)
        out = self.embed_v1(out_0)
        out_all1 = torch.cat((out,out_hat1),dim=1)
        out_all1 = self.cont1(out_all1)
        out = out_all1
        out_hat2 = self.HAT(out_all1)



        w0 = self.linear_0(out).view(B, -1) #w0[32,2000]

        out_g = self.gcn(out, w0.detach()) #out_g[32,128,2000,1]
        out = out_g + out

        out = self.embed_1(out)
        out_all2 = torch.cat((out,out_hat2),dim=1)
        out_all2 = self.cont2(out_all2)
        out_hat3 = self.HAT(out_all2)
        out_all3 = torch.cat((out_hat3,out),1)
        out_all3 = self.cont3(out_all3)

        w1 = self.linear_1(out_all3).view(B, -1)
        # w1 = self.linear_1(out).view(B, -1) #w1[32,2000]

        if self.predict == False: #剪枝，不预测
            w1_ds, indices = torch.sort(w1, dim=-1, descending=True) #w1排序,w1_ds[32,2000],indices[32,2000]是索引
            w1_ds = w1_ds[:, :int(N*self.sr)] #w1_ds[32,1000]剪枝？剪掉一半 self.sr=0.5
            x_ds, y_ds, w0_ds = self.down_sampling(x, y, w0, indices, None, self.predict)
            #x_ds[32,1,1000,4],y_ds[32,1000],w0_ds[32,1000],ds：剪枝后？
            return x_ds, y_ds, [w0, w1], [w0_ds, w1_ds]
        else: #剪枝，出预测结果
            w1_ds, indices = torch.sort(w1, dim=-1, descending=True) #w1排序,w1_ds[32,1000],indices[32,1000]是索引
            w1_ds = w1_ds[:, :int(N*self.sr)] #w1_ds[32,500]剪枝？剪掉一半 self.sr=0.5
            x_ds, y_ds, w0_ds, out = self.down_sampling(x, y, w0, indices, out_all3, self.predict)
            # x_ds[32,1,500,4],y_ds[32,500],w0_ds[32,500],out[32,128,500,1]也是剪枝后,ds：剪枝后？
            out = self.embed_2(out)
            w2 = self.linear_2(out) #[32,2,500,1]
            e_hat = weighted_8points(x_ds, w2)

            return x_ds, y_ds, [w0, w1, w2[:, 0, :, 0]], [w0_ds, w1_ds], e_hat

class CLNet(nn.Module):
    def __init__(self, config):
        super(CLNet, self).__init__()

        self.ds_0 = DS_Block(initial=True, predict=False, out_channel=128, k_num=9, sampling_rate=config.sr)#sampling_rate=0.5
        self.ds_1 = DS_Block(initial=False, predict=True, out_channel=128, k_num=6, sampling_rate=config.sr)

    def forward(self, x, y):
        #x[32,1,2000,4],y[32,2000]
        B, _, N, _ = x.shape

        x1, y1, ws0, w_ds0 = self.ds_0(x, y) # 返回的是x_ds, y_ds, [w0, w1], [w0_ds, w1_ds]

        w_ds0[0] = torch.relu(torch.tanh(w_ds0[0])).reshape(B, 1, -1, 1) #变成0到1的权重[32,1,1000,1]
        w_ds0[1] = torch.relu(torch.tanh(w_ds0[1])).reshape(B, 1, -1, 1) #变成0到1的权重[32,1,1000,1]
        x_ = torch.cat([x1, w_ds0[0].detach(), w_ds0[1].detach()], dim=-1) #x_[32,1,1000,6] 剪枝后的特征并带上了权重信息

        x2, y2, ws1, w_ds1, e_hat = self.ds_1(x_, y1) #x_[32,1,1000,6],y1[32,1000]

        with torch.no_grad():
            y_hat = batch_episym(x[:, 0, :, :2], x[:, 0, :, 2:], e_hat) #y_hat对称极线距离
        #print(y_hat)
        return ws0 + ws1, [y, y, y1, y1, y2], [e_hat], y_hat


class TFC(nn.Module):
    def __init__(self, in_channel, out_channel, num_patch, p_size, emb_dropout, T_depth, heads, dim_head, mlp_dim, dropout = 0.1):
        super(TFC, self).__init__()
        self.p_size = p_size

        self.patch_to_embedding = nn.Linear(in_channel, out_channel)
        self.cls_token = nn.Parameter(torch.randn(1, 1, out_channel))#20220316lyp 通过Parameter来生成一个可学习的变量
        # self.pos_embedding = nn.Parameter(torch.randn(1, num_patch + 1, out_channel))
        self.dropout = nn.Dropout(emb_dropout)
        self.transformer = Transformer(out_channel, T_depth, heads, dim_head, mlp_dim, dropout)
        self.to_latent = nn.Identity()

        # self.NeA = Bottleneck(out_channel, out_channel//4)

    def forward(self, x, mask=None):
        _,_,hh,ww = x.size()
        x = rearrange(x, 'b c (h p1) (w p2) -> b (h w) (p1 p2 c)', p1=self.p_size, p2=self.p_size)#20220316lyp 输出结果是bx(hxw)x(p1xp2xc)  32x128x256
        x = self.patch_to_embedding(x)
        b, n, _ = x.size()#20220316lyp b=32,n=128,_=256
        cls_tokens = repeat(self.cls_token, '() n d -> b n d', b=b)#20220316lyp 32x1x256
        x = torch.cat((cls_tokens, x), dim=1)#20220316lyp x:32x129x256
        # x += self.pos_embedding[:, :(n + 1)]#20220316lyp 取第二维的前(n+1)个
        x = self.dropout(x)
        x = self.transformer(x)#20220317lyp [32,129,256]
        x_mid = x[:, 0]#20220317lyp 将cls_tokens取出来(第一列就是cls_tokens) 取出第二维的第0行的所有数据
        x_mid = self.to_latent(x_mid)#20220317lyp 不区分参数的占位符标识运算符 即输入数据是什么，输出也仍然是什么，不改变值和维度
        x = rearrange(x[:, 1:], 'b (h w) (p1 p2 c) -> b c (h p1) (w p2)', p1=self.p_size, p2=self.p_size, h=hh, w=ww)#20220317lyp 改变x的维度为[32,256,16,8]
        # x = self.NeA(x)

        return x, x_mid

class HAT(nn.Module):
    def __init__(self, in_dim=128, poi_dim=2048, heads=2, mlp_dim=2048, tfc_depth=2, dim_head=64):#20220316lyp heads=16, mlp_dim=2048,
        super(HAT, self).__init__()

        ###################### parameters
        T_depth1=2#, T_depth2, T_depth3 = tfc_depth#20220316lyp TFC_DEPTH : [3, 3, 6]
        # inc1, inc2, inc3 = in_dim#20220316lyp IN_DIM: [256, 512, 1024]
        inc1 = in_dim

        self.TFC_S1 = TFC(in_channel=inc1, out_channel=inc1, num_patch=128, p_size=1, emb_dropout=0.1, T_depth=T_depth1,
                          heads=heads, dim_head=dim_head, mlp_dim=mlp_dim, dropout=0.1)
        # self.TFC_S2 = TFC(in_channel=(inc1+inc2), out_channel=(inc1+inc2), num_patch=128, p_size=1, emb_dropout=0.1, T_depth=T_depth2,
        #                   heads=heads, dim_head=dim_head, mlp_dim=mlp_dim, dropout=0.1)
        # self.TFC_S3 = TFC(in_channel=(inc1+inc2+inc3), out_channel=poi_dim, num_patch=128, p_size=1, emb_dropout=0.1, T_depth=T_depth3,
        #                   heads=heads, dim_head=dim_head, mlp_dim=mlp_dim, dropout=0.1)


    def forward(self, x1,mask=None):

        x1, x_mid_1 = self.TFC_S1(x1)#20220317lyp x1的维度是[32,256,16,8]; x_mid_1:[32,256](x_mid_1是cls_tokens)
        # x2, x_mid_2 = self.TFC_S2(torch.cat((x2 ,x1), dim=1))#20220318lyp x2维度是[32,768,16,8];x_mid_2:[32,768]
        # x3, x_mid_3 = self.TFC_S3(torch.cat((x3 ,x2), dim=1))#20220318lyp x3维度是[32,2048,16,8];x_mid_3:[32,2048]

        return x1   #, x_mid_2, x_mid_3

