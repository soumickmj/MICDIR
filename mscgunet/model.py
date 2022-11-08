import torch
import torch.nn as nn
import torch.nn.functional as F

class Feature_Extractor(nn.Module):
  def __init__(self,in_channel, n_classes, start_channel):
        self.in_channel = in_channel
        self.n_classes = n_classes
        self.start_channel = start_channel
        super(Feature_Extractor, self).__init__()
        self.en1 = self.encoder(self.in_channel, self.start_channel, bias=False) #128, 16

        self.en2 = self.encoder(self.start_channel * 1, self.start_channel * 2, stride=2, bias=False) #64, 32
        self.en3 = self.encoder(self.start_channel * 2, self.start_channel * 2, stride=2, bias=False) #32, 32
        self.en4 = self.encoder(self.start_channel * 2, self.start_channel * 2, stride=2, bias=False) #16, 32
        self.en5 = self.encoder(self.start_channel * 2, self.start_channel * 2, stride=2, bias=False) #8, 32
        self.en6 = self.encoder(self.start_channel * 2, self.start_channel * 2, stride=2, bias=False) #4, 32

  def encoder(self, in_channels, out_channels, kernel_size=3, stride=1, padding=1, bias=False):
    return nn.Sequential(
        nn.Conv3d(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            bias=bias,
        ),
        nn.LeakyReLU(0.2),
    )

  def forward(self, x,y):
        x_in=torch.cat((x, y), 1)
        enc_1 = self.en1(x_in)
        enc_2 = self.en2(enc_1)
        enc_3 = self.en3(enc_2)
        enc_4 = self.en4(enc_3) # 16
        enc_5 = self.en5(enc_4) # 8
        enc_6 = self.en6(enc_5) # 4
        return enc_1, enc_2, enc_3, enc_4, enc_5, enc_6

class convEncoder(nn.Module):
      def __init__(self, encoder_in_channel, decoder_in_channel, out_channel=3):
          self.encoder_in_channel = encoder_in_channel
          self.decoder_in_channel = decoder_in_channel
          self.out_channel = out_channel
          super(convEncoder, self).__init__()
          self.op = self.decoder(self.encoder_in_channel + self.decoder_in_channel, self.out_channel)

      def decoder(self, in_channels, out_channels, kernel_size=3, stride=1, padding=1, bias=False):
        return nn.Sequential(
            nn.Conv3d(
                in_channels, out_channels, kernel_size, bias=bias, padding=padding),
            nn.LeakyReLU(0.2),
        )

      def forward(self, decoder_feats, encoder_feats):
        B, C, H, W, D = encoder_feats.size()
        interpolation_feats = F.interpolate(decoder_feats, (H, W, D), mode='trilinear', align_corners=False)
        concatenated_feats = torch.cat((interpolation_feats, encoder_feats), 1)
        return self.op(concatenated_feats)


class convDecoder(nn.Module):
    def __init__(self, in_channel, out_channel=3):
        self.in_channel = in_channel
        self.out_channel = out_channel
        super(convDecoder, self).__init__()
        self.op = self.output(self.in_channel, self.out_channel)

    def output(self, in_channels, out_channels, kernel_size=3, padding=1, bias=False, batchnorm=True):
      return nn.Sequential(
          nn.Conv3d(
              in_channels, out_channels, kernel_size, bias=bias, padding=padding),
          nn.LeakyReLU(0.2),
      )

    def conv1X1(self, in_channels, out_channels, kernel_size=1, padding=0, bias=False, batchnorm=False):
      return nn.Sequential(
          nn.Conv3d(
              in_channels, out_channels, kernel_size, bias=bias, padding=padding),
          nn.InstanceNorm3d(out_channels, affine=True),
          nn.LeakyReLU(0.2),
      )

    def forward(self, latent_feats):
        return self.op(latent_feats)


class SCG_block(nn.Module):
    def __init__(self, in_ch, hidden_ch=9, node_size=(8, 8, 8), add_diag=True, dropout=0.2):
        super(SCG_block, self).__init__()
        self.node_size = node_size
        self.hidden = hidden_ch
        self.nodes = node_size[0]*node_size[1]*node_size[2]
        self.add_diag = add_diag
        self.pool = nn.AdaptiveAvgPool3d(node_size)
        self.lkrelu = nn.LeakyReLU(0.2)

        self.mu = nn.Sequential(
            nn.Conv3d(in_ch, hidden_ch, 3, padding=1, bias=True),
            nn.Dropout(dropout),
        )

        self.logvar = nn.Sequential(
            nn.Conv3d(in_ch, hidden_ch, 1, 1, bias=True),
            nn.Dropout(dropout),
        )

    def forward(self, x):
      B, C, H, W, D = x.size()
      gx = self.pool(x)  # mean matrix

      mu, log_var = self.mu(gx), self.logvar(gx)  #logvar is the standard dev matrix

      if self.training:
          std = torch.exp(log_var.reshape(B, self.nodes, self.hidden))
          eps = torch.randn_like(std)
          z = mu.reshape(B, self.nodes, self.hidden) + std*eps
      else:
          z = mu.reshape(B, self.nodes, self.hidden)


#decoder block C: In the DEC-block, the graph adjacency matrix A is generated by an inner product between latent embeddings as A = ReLU(ZZT).
      A = torch.matmul(z, z.permute(0, 2, 1))
      A = self.lkrelu(A)
      Ad = torch.diagonal(A, dim1=1, dim2=2)
      mean = torch.mean(Ad, dim=1)
      gama = torch.sqrt(1 + 1.0 / mean).unsqueeze(-1).unsqueeze(-1) # equation for finding gamma used in equation 10

      dl_loss = gama.mean() * torch.log(Ad[Ad<1]+ 1.e-7).sum() / (A.size(0) * A.size(1) * A.size(2)) # equation 10

      kl_loss = -0.5 / self.nodes * torch.mean(
          torch.sum(1 + 2 * log_var - mu.pow(2) - log_var.exp().pow(2), 1)  # equation 9
      )
      loss = kl_loss - dl_loss

      if self.add_diag:
        diag = [torch.diag(Ad[i, :]).unsqueeze(0) for i in range(Ad.shape[0])]
        A = A + gama * torch.cat(diag, 0)
      A = self.laplacian_matrix(A, self_loop=True)
      z_hat = gama.mean() * \
                  mu.reshape(B, self.nodes, self.hidden) * \
                  (1. - log_var.reshape(B, self.nodes, self.hidden))

      return A, gx, loss, z_hat

    @classmethod
    def laplacian_matrix(cls, A, self_loop=False):
      '''
        Computes normalized Laplacian matrix: A (B, N, N)
        '''
      if self_loop:
          A = A + torch.eye(A.size(1), device=A.device).unsqueeze(0)
      deg_inv_sqrt = (torch.sum(A, 1) + 1e-5).pow(-0.5)
      return deg_inv_sqrt.unsqueeze(-1) * A * deg_inv_sqrt.unsqueeze(-2)


class GCN_Layer(nn.Module):
    def __init__(self, in_features, out_features, bnorm=True, activation=nn.LeakyReLU(0.2), dropout=None):
        super(GCN_Layer, self).__init__()
        self.bnorm = bnorm
        fc = [nn.Linear(in_features, out_features)]
        if bnorm:
            fc.append(BatchNorm_GCN(out_features))
        if activation is not None:
            fc.append(activation)
        if dropout is not None:
            fc.append(nn.Dropout(dropout))
        self.fc = nn.Sequential(*fc)

    def forward(self, data):
        x, A = data
        tbmm = torch.bmm(A, x)
        y = self.fc(tbmm)

        return [y, A]


def weight_xavier_init(*models):
  for model in models:
    for module in model.modules():
      if isinstance(module, (nn.Conv2d, nn.Linear)):
        nn.init.orthogonal_(module.weight)
        if module.bias is not None:
            module.bias.data.zero_()
      elif isinstance(module, nn.BatchNorm2d):
          module.weight.data.fill_(1)
          module.bias.data.zero_()


class BatchNorm_GCN(nn.BatchNorm1d):
    '''Batch normalization over GCN features'''

    def __init__(self, num_features):
        super(BatchNorm_GCN, self).__init__(num_features)

    def forward(self, x):
        return super(BatchNorm_GCN, self).forward(x.permute(0, 2, 1)).permute(0, 2, 1)