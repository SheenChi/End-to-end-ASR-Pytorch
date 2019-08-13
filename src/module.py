import torch
import numpy as np
import torch.nn as nn

class VGGExtractor(nn.Module):
    ''' VGG extractor for ASR described in https://arxiv.org/pdf/1706.02737.pdf'''
    def __init__(self,input_dim):
        super(VGGExtractor, self).__init__()
        in_channel,freq_dim,out_dim = self.check_dim(input_dim)
        self.in_channel = in_channel
        self.freq_dim = freq_dim
        self.out_dim = out_dim

        self.extractor = nn.Sequential(
                                nn.Conv2d(in_channel, 64, 3, stride=1, padding=1),
                                nn.ReLU(),
                                nn.Conv2d(    64, 64, 3, stride=1, padding=1),
                                nn.ReLU(),
                                nn.MaxPool2d(2, stride=2), # Half-time dimension
                                nn.Conv2d(    64,128, 3, stride=1, padding=1),
                                nn.ReLU(),
                                nn.Conv2d(   128,128, 3, stride=1, padding=1),
                                nn.ReLU(),
                                nn.MaxPool2d(2, stride=2) # Half-time dimension
                            )

    def check_dim(self,input_dim):
        # Check input dimension, delta feature should be stack over channel. 
        input_dim = example_input.shape[-1]
        if input_dim%13 == 0:
            # MFCC feature
            return int(input_dim/13),13,(13//4)*128
        elif input_dim%40 == 0:
            # Fbank feature
            return int(input_dim/40),40,(40//4)*128
        else:
            raise ValueError('Acoustic feature dimension for VGG should be 13/26/39(MFCC) or 40/80/120(Fbank) but got '+d)

    def view_input(self,feature,feat_len):
        # downsample time
        feat_len = feat_len//4
        # crop sequence s.t. t%4==0
        if feature.shape[1]%4 != 0:
            feature = feature[:,:-(feature.shape[1]%4),:].contiguous()
        bs,ts,ds = feature.shape
        # stack feature according to result of check_dim
        feature = feature.view(bs,ts,self.in_channel,self.freq_dim)
        feature = feature.transpose(1,2)

        return feature,feat_len

    def forward(self,feature,feat_len):
        # Feature shape BSxTxD -> BS x CH(num of delta) x T x D(acoustic feature dim)
        feature, feat_len = self.view_input(feature,feat_len)
        # Foward
        feature = self.extractor(feature)
        # BSx128xT/4xD/4 -> BSxT/4x128xD/4
        feature = feature.transpose(1,2)
        #  BS x T/4 x 128 x D/4 -> BS x T/4 x 32D
        feature = feature.contiguous().view(feature.shape[0],feature.shape[1],self.out_dim)
        return feature,feat_len


class RNNLayer(nn.Module):
    ''' RNN wrapper, includes time-downsampling'''
    def __init__(self, input_dim, module, dim, bidirection, dropout, layer_norm, sample_rate, sample_style):
        super(RNNLayer, self).__init__()
        # Setup
        rnn_out_dim = 2*dim if bidirection else dim
        self.out_dim = sample_rate*rnn_out_dim if sample_rate>1 and sample_style=='concat' else rnn_out_dim
        self.dropout = dropout
        self.layer_norm = layer_norm
        self.sample_rate = sample_rate
        self.sample_style = sample_style

        if self.sample_style not in ['drop','concat']:
            raise ValueError('Unsupported Sample Style: '+self.sample_style)
        
        # Recurrent layer
        self.layer = getattr(nn,rnn_cell.upper())(in_dim,dim, bidirectional=bidirection, num_layers=1, batch_first=True)

        # Regularizations
        if self.layer_norm:
            self.ln = nn.LayerNorm(rnn_out_dim)
        if self.dropout>0:
            self.dp = nn.Dropout(p=dropout)

    
    def forward(self, input_x , x_len):
        # Forward RNN
        # ToDo: check time efficiency of pack/pad
        input_x = pack_padded_sequence(input_x, x_len, batch_first=True)
        output,_ = self.layer(input_x,state)
        output,x_len = pad_packed_sequence(output,batch_first=True)

        # Normalizations
        if self.layer_norm:
            output = self.ln(output)
        if self.dropout>0:
            output = self.dp(output)

        # Perform Downsampling
        if self.sample_rate > 1:
            batch_size,timestep,feature_dim = output.shape
            x_len = x_len//self.sample_rate

            if self.sample_style =='drop':
                # Drop the unselected timesteps
                output = output[:,::self.sample_rate,:].contiguous()
            else:
                # Drop the redundant frames and concat the rest according to sample rate
                if timestep%self.sample_rate != 0:
                    output = output[:,:-(timestep%self.sample_rate),:]
                output = output.contiguous().view(batch_size,int(timestep/self.sample_rate),feature_dim*self.sample_rate)

        return output,x_len

class ScaleDotAttention(nn.module):
    ''' Scaled Dot-Product Attention '''
    def __init__(self, temperature):
        super().__init__()
        self.temperature = temperature
        self.softmax = nn.Softmax(dim=1)

    def forward(self, q, k, v, mask=None):

        attn = torch.bmm(q, k.transpose(1, 2)) # BNxD * BNxDxT = BNxT
        attn = attn / self.temperature

        if mask is not None:
            attn = attn.masked_fill(mask, -np.inf)

        attn = self.softmax(attn) # BNxT
        output = torch.bmm(attn, v) # BNxT x BNxTxD-> BNxD
        attn = attn.view(bs,self.num_head,-1) # BNxT -> BxNxT

        return output, attn

class LocationAwareAttention(nn.module):
    ''' Location-Awared Attention '''
    def __init__(self, kernel_size, kernel_num, dim, num_head, temperature):
        super().__init__()
        self.temperature = temperature
        self.num_head = num_head
        self.softmax = nn.Softmax(dim=1)
        self.prev_att  = None
        self.loc_conv = nn.Conv1d(num_head, kernel_num, kernel_size=2*kernel_size+1, padding=kernel_size, bias=False)
        self.loc_proj = nn.Linear(kernel_num, dim,bias=False)
        self.gen_energy = nn.Linear(dim, 1)

    def reset_mem(self):
        self.prev_att = None


    def forward(self, q, k, v, mask=None):
        ts = k.shape[1]
        # Uniformly init prev_att
        if self.prev_att is None:
            self.prev_att = torch.zeros((bs,self.num_head,ts)).to(k.device)
            for idx,sl in enumerate(enc_len):
                self.prev_att[idx,:,:sl] = 1.0/sl

        # Calculate location context
        loc_context = torch.tanh(self.loc_proj(self.loc_conv(self.prev_att).transpose(1,2))) # BxNxT->BxTxD
        loc_context = loc_context.unsqueeze(1).repeat(1,self.num_head,1,1).view(-1,ts,dim)   # BxNxTxD -> BNxTxD
        q = q.unsqueeze(1) # BNx1xD
        
        # Compute energy and context
        energy = self.gen_energy(torch.tanh( self.k+q+loc_context )).squeeze(2) # BNxTxD -> BNxT
        energy = energy / self.temperature
        if mask is not None:
            energy.masked_fill_(mask, -np.inf)
        attn = self.softmax(attn)
        output = torch.bmm(attn, v) # BNxT x BNxTxD-> BNxD
        attn = attn.view(bs,self.num_head,-1) # BNxT -> BxNxT
        self.prev_att = attn

        return output, attn